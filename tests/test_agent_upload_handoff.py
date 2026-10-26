"""Agent-local bytes reach a session-bound hold without a second credential."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import httpx
import pytest
from fastmcp import FastMCP

from exomem import auth_sessions, held_uploads, server_auth, server_transfer
from exomem.__main__ import _attach_main
from exomem.governance import principal


@pytest.fixture
def upload_env(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")
    monkeypatch.delenv("EXOMEM_UPLOAD_TOKEN", raising=False)
    monkeypatch.delenv("EXOMEM_CF_ACCESS_TEAM_DOMAIN", raising=False)
    authority = auth_sessions.SessionAuthority.local(
        directory=tmp_path / "sessions", signing_root="synthetic-private-signing-key",
        issuer="https://capture.example", audience="https://capture.example/mcp",
    )
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **_: authority)
    bearer, record = asyncio.run(authority.issue(
        client_id="agent", scopes=["mcp"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
    ))
    who = principal.RequestPrincipal(
        audience_id="owner", surface="mcp", issuer_family="mcp-oauth:synthetic",
        remote_owner=True,
        client_binding=principal.ClientBinding(authority.issuer, record.client_id),
        origin_session=principal.OriginSessionBinding(
            record.session_id, record.generation, record.audience,
        ),
    )
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline)
    app = FastMCP("upload-handoff-test")
    server_transfer.register_transfer_routes(app, vault_root=vault, media_worker=None)
    return vault, who, authority, bearer, app.http_app()


def _prepare(env, data=b"exact original\n", **kwargs):
    from exomem import upload_handoff
    vault, who, *_ = env
    with principal.request_scope(who):
        return upload_handoff.prepare_upload(
            vault, lane="source", expected_sha256=hashlib.sha256(data).hexdigest(), **kwargs,
        )


def _post(env, descriptor, data=b"exact original\n", **fields):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env[-1]), base_url="https://capture.example",
        ) as client:
            return await client.post(
                "/upload", headers={"Authorization": "Bearer " + descriptor["token"]},
                files={"file": ("original.txt", data, "text/plain")},
                data={"hold": "1", "lane": "source", **fields},
            )
    return asyncio.run(send())


class _ASGISyncTransport(httpx.BaseTransport):
    def __init__(self, app):
        self.app = app

    def handle_request(self, request):
        body = request.read()
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                return await client.request(
                    request.method, str(request.url), headers=request.headers, content=body,
                )
        response = asyncio.run(send())
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)


@pytest.mark.parametrize("endpoint", ["https://capture.example/upload", "http://127.0.0.1:9123/upload"])
def test_cli_handoff_holds_exact_bytes_for_only_the_issuing_session(upload_env, tmp_path, capsys, endpoint):
    original = b"timestamp,reading\n00:01:12,7\n\x00\xff"
    descriptor = _prepare(upload_env, original)
    descriptor["upload_url"] = endpoint
    assert descriptor["committed"] is False
    assert descriptor["handoff_status"] == "handoff_prepared"
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps(descriptor))
    source = tmp_path / "original.bin"
    source.write_bytes(original)
    result = _attach_main(
        ["--handoff-file", str(handoff), "--root", str(tmp_path), str(source)],
        transport=_ASGISyncTransport(upload_env[-1]),
    )
    assert result == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["hash"] == hashlib.sha256(original).hexdigest()
    vault, who, *_ = upload_env
    with principal.request_scope(replace(who, origin_session=replace(who.origin_session, session_id="other"))):
        with pytest.raises(held_uploads.HeldUploadError, match="no redeemable"):
            held_uploads.redeem(vault, receipt["file"]["download_url"], lane="source")
    with principal.request_scope(who):
        redeemed = held_uploads.redeem(vault, receipt["file"]["download_url"], lane="source")
        assert redeemed.path.read_bytes() == original
        held_uploads.restore(vault, redeemed)
        retried = held_uploads.redeem(vault, receipt["file"]["download_url"], lane="source")
        retried.path.unlink()
    assert _post(upload_env, descriptor, original).status_code == 401


def test_revoked_expired_and_foreign_tickets_share_one_refusal(upload_env, monkeypatch):
    from exomem import upload_handoff
    revoked = _prepare(upload_env)
    expired = _prepare(upload_env)
    asyncio.run(upload_env[2].revoke_bearer(upload_env[3], reason="test"))
    refused = _post(upload_env, revoked)
    assert refused.status_code == 401
    monkeypatch.setattr(upload_handoff, "_now", lambda: 10**12)
    timed_out = _post(upload_env, expired)
    foreign = _post(upload_env, {"token": upload_handoff.TOKEN_PREFIX + "x" * 43})
    assert refused.json() == timed_out.json() == foreign.json()


@pytest.mark.parametrize("fields", [{"lane": "evidence"}, {"hold": ""}])
def test_ticket_cannot_change_lane_or_commit_directly(upload_env, fields):
    response = _post(upload_env, _prepare(upload_env), **fields)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_UPLOAD"


def test_hash_mismatch_publishes_no_held_bytes(upload_env):
    response = _post(upload_env, _prepare(upload_env), b"altered original")
    assert response.status_code == 400
    assert response.json()["code"] == "SOURCE_HASH_MISMATCH"
    store = held_uploads._store(upload_env[0])
    assert not list(store.glob("*.bin"))
    assert not list(store.glob("*.json"))


def test_chunked_body_is_bounded_before_multipart_spooling(upload_env):
    descriptor = _prepare(upload_env, max_bytes=16)
    async def send():
        delivered = 0
        async def chunks():
            nonlocal delivered
            yield b'--bounded\r\nContent-Disposition: form-data; name="file"; filename="large.bin"\r\nContent-Type: application/octet-stream\r\n\r\n'
            for _ in range(8):
                delivered += 1
                yield b"x" * (256 * 1024)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upload_env[-1]), base_url="https://capture.example",
        ) as client:
            response = await client.post(
                "/upload", headers={"Authorization": "Bearer " + descriptor["token"],
                    "Content-Type": "multipart/form-data; boundary=bounded"}, content=chunks(),
            )
        assert response.status_code == 413
        assert delivered < 8
    asyncio.run(send())


@pytest.mark.parametrize("unsafe", ["../outside.bin", "alias/original.bin", "leaf.bin"])
def test_cli_rejects_traversal_and_symlinks_before_sending(upload_env, tmp_path, unsafe):
    original = b"exact original\n"
    source = tmp_path / "original.bin"
    source.write_bytes(original)
    try:
        if unsafe == "alias/original.bin":
            (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        elif unsafe == "leaf.bin":
            (tmp_path / "leaf.bin").symlink_to(source)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:
            pytest.skip("the host does not grant symlink creation")
        raise
    descriptor = tmp_path / "handoff.json"
    descriptor.write_text(json.dumps(_prepare(upload_env, original)))
    def never_send(request):
        pytest.fail("unsafe file reached network")
    assert _attach_main(
        ["--handoff-file", str(descriptor), "--root", str(tmp_path), unsafe],
        transport=httpx.MockTransport(never_send),
    ) == 2


def test_cli_refuses_changed_bytes_before_upload(upload_env, tmp_path):
    descriptor = tmp_path / "handoff.json"
    descriptor.write_text(json.dumps(_prepare(upload_env)))
    source = tmp_path / "original.bin"
    source.write_bytes(b"changed")
    def never_send(request):
        pytest.fail("hash mismatch reached network")
    assert _attach_main(
        ["--handoff-file", str(descriptor), "--root", str(tmp_path), str(source)],
        transport=httpx.MockTransport(never_send),
    ) == 2


def test_cli_checks_the_server_receipt_before_returning_a_handle(upload_env, tmp_path, capsys):
    source = tmp_path / "original.bin"
    source.write_bytes(b"exact original\n")
    descriptor = tmp_path / "handoff.json"
    descriptor.write_text(json.dumps(_prepare(upload_env)))
    def changed_receipt(request):
        return httpx.Response(201, json={"state": "held", "size": 0, "hash": "0" * 64,
            "file": {"download_url": "exomem-held:wrong"}})
    assert _attach_main(
        ["--handoff-file", str(descriptor), "--root", str(tmp_path), str(source)],
        transport=httpx.MockTransport(changed_receipt),
    ) == 1
    assert capsys.readouterr().out == ""


def test_unverified_owner_cannot_issue_a_ticket(upload_env):
    from exomem import upload_handoff
    with principal.request_scope(principal.owner_principal(surface="mcp")):
        with pytest.raises(ValueError, match="UPLOAD_HANDOFF_UNAVAILABLE:.*personal HTTP MCP"):
            upload_handoff.prepare_upload(upload_env[0], lane="source", expected_sha256="0" * 64)


@pytest.mark.parametrize("endpoint", [
    "http://capture.example/upload", "https://capture.example/other",
    "https://user:secret@capture.example/upload", "https://capture.example/upload?token=x",
    "https://capture.example/upload#fragment", "http://localhost/upload",
    "https://capture.example/upload?", "https://capture.example/upload#",
])
def test_cli_refuses_handoffs_that_could_send_credentials_elsewhere(upload_env, tmp_path, endpoint):
    source = tmp_path / "original.bin"
    source.write_bytes(b"exact original\n")
    handoff = _prepare(upload_env)
    handoff["upload_url"] = endpoint
    descriptor = tmp_path / "handoff.json"
    descriptor.write_text(json.dumps(handoff))
    def never_send(request):
        pytest.fail("unsafe endpoint reached network")
    assert _attach_main(
        ["--handoff-file", str(descriptor), "--root", str(tmp_path), str(source)],
        transport=httpx.MockTransport(never_send),
    ) == 2


def test_cli_does_not_follow_redirects_with_the_upload_ticket(upload_env, tmp_path):
    source = tmp_path / "original.bin"
    source.write_bytes(b"exact original\n")
    descriptor = tmp_path / "handoff.json"
    descriptor.write_text(json.dumps(_prepare(upload_env)))
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.example/upload"})
    assert _attach_main(
        ["--handoff-file", str(descriptor), "--root", str(tmp_path), str(source)],
        transport=httpx.MockTransport(redirect),
    ) == 1
    assert calls == ["https://capture.example/upload"]


def test_file_size_cannot_exceed_the_ticket_limit(upload_env):
    response = _post(upload_env, _prepare(upload_env, max_bytes=4))
    assert response.status_code == 413
    assert response.json()["code"] == "TOO_LARGE"
    assert not list(held_uploads._store(upload_env[0]).glob("*.bin"))


def test_ticket_cannot_cross_deployments(upload_env, monkeypatch):
    from exomem import upload_handoff
    ticket = _prepare(upload_env)
    monkeypatch.setattr(upload_handoff.artifact_recovery, "deployment", lambda _: "another-deployment")
    response = _post(upload_env, ticket)
    assert response.status_code == 401
    assert response.json()["reason"] == upload_handoff.UNAVAILABLE_REASON


def test_only_one_concurrent_request_can_claim_a_ticket(upload_env):
    descriptor = _prepare(upload_env)
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upload_env[-1]), base_url="https://capture.example",
        ) as client:
            async def post():
                return await client.post(
                    "/upload", headers={"Authorization": "Bearer " + descriptor["token"]},
                    files={"file": ("original.txt", b"exact original\n", "text/plain")},
                    data={"hold": "1", "lane": "source"},
                )
            responses = await asyncio.gather(post(), post())
            assert sorted(response.status_code for response in responses) == [201, 401]
    asyncio.run(send())
