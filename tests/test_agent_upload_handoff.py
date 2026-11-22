"""Agent-local bytes reach a session-bound hold without a second credential."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO

import httpx
import pytest
from _curl_send import curl_request
from fastmcp import FastMCP

from exomem import auth_sessions, held_uploads, server_auth, server_transfer
from exomem.__main__ import _attach_main
from exomem.governance import principal


@pytest.fixture
def upload_env(vault, tmp_path, monkeypatch):
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://capture.example")
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", "42")
    monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", "github:42")
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


def _prepare(env, data=b"exact original\n", *, lane="source", **kwargs):
    from exomem import upload_handoff
    vault, who, *_ = env
    with principal.request_scope(who):
        return upload_handoff.prepare_upload(
            vault, lane=lane, expected_sha256=hashlib.sha256(data).hexdigest(), **kwargs,
        )


def _post(env, descriptor, data=b"exact original\n", *, filename="original.txt", **fields):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env[-1]), base_url="https://capture.example",
        ) as client:
            return await client.post(
                "/upload", headers={"Authorization": "Bearer " + descriptor["token"]},
                files={"file": (filename, data, "text/plain")},
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


def test_upload_files_selects_a_resumable_handoff_for_a_large_source(upload_env):
    from exomem import commands, public_tools, upload_handoff, upload_sessions

    vault, who, *_ = upload_env
    size = upload_handoff.MAX_BYTES + 1
    digest = hashlib.sha256(b"sample original").hexdigest()
    wire = public_tools.UploadFiles(lane="source", expected_sha256=digest, size_bytes=size)
    leaf, arguments = public_tools._upload_files(wire.model_dump(exclude_none=True), None)

    with principal.request_scope(who):
        assert leaf == "transfer_artifact"
        handoff = commands.op_transfer_artifact(vault, **arguments)

    assert handoff["committed"] is False and handoff["expected_sha256"] == digest
    assert handoff["size_bytes"] == size
    assert handoff["resumable"]["create_url"] == "https://capture.example" + upload_sessions.SESSIONS_PATH


def test_large_source_respects_the_session_limit_and_an_explicit_hold(upload_env, monkeypatch):
    from exomem import upload_handoff, upload_sessions

    limit = upload_handoff.MAX_BYTES + 1
    monkeypatch.setenv(upload_sessions.MAX_BYTES_ENV, str(limit))
    with pytest.raises(ValueError, match=f"TOO_LARGE: a Source session holds at most {limit:,} bytes"):
        _prepare(upload_env, size_bytes=limit + 1)
    with pytest.raises(ValueError, match=f"TOO_LARGE: this handoff holds at most {upload_handoff.MAX_BYTES:,} bytes"):
        _prepare(upload_env, size_bytes=limit, held_only=True)
    assert _prepare(upload_env)["hold"] is True


def test_resumable_ticket_cannot_be_read_as_a_legacy_multipart_ticket(upload_env, monkeypatch):
    from exomem import private_state, upload_handoff

    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    vault = upload_env[0]
    data = b"recording"
    handoff = _prepare(upload_env, data, size_bytes=len(data))
    legacy = _prepare(upload_env, b"legacy")

    # A downgraded worker locates a multipart ticket by nonce in the original store.
    legacy_store = private_state.store(vault, upload_handoff.STORE_DIRNAME)
    legacy_nonce = upload_handoff.parse_ticket(legacy["token"])
    delegated_nonce = upload_handoff.parse_ticket(handoff["token"], resumable=True)
    assert legacy_nonce is not None and delegated_nonce is not None
    assert (legacy_store / (hashlib.sha256(legacy_nonce.encode()).hexdigest() + ".json")).is_file()
    assert not (legacy_store / (hashlib.sha256(delegated_nonce.encode()).hexdigest() + ".json")).exists()
    assert _post(upload_env, legacy, b"legacy").status_code == 201


def test_resumable_ticket_survives_access_refresh(upload_env, monkeypatch):
    from exomem import upload_handoff

    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    vault, who, authority, _, _ = upload_env
    clock = [time.time()]
    monkeypatch.setattr(authority, "clock", lambda: clock[0])
    access, record, refresh = asyncio.run(authority.issue_offline(
        client_id=who.client_binding.client_id, scopes=["mcp", "offline_access"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
    ))
    issuer = replace(who, origin_session=principal.OriginSessionBinding(
        record.session_id, record.generation, record.audience,
    ))
    data = b"recording"
    digest = hashlib.sha256(data).hexdigest()
    assert record.expires_at is not None
    clock[0] = record.expires_at - 1
    with principal.request_scope(issuer):
        handoff = upload_handoff.prepare_upload(vault, lane="source", expected_sha256=digest, size_bytes=len(data))

    clock[0] += 2
    refreshed, _, _ = asyncio.run(authority.rotate_refresh(refresh, client_id=record.client_id, scopes=["mcp"]))
    assert asyncio.run(authority.validate(access)) is None
    assert asyncio.run(authority.validate(refreshed)) is not None
    claimed = asyncio.run(upload_handoff.claim_upload(
        vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
    ))
    assert claimed["principal"] == issuer


def test_resumable_ticket_rechecks_the_current_owner_before_spending(upload_env, monkeypatch):
    from exomem import upload_handoff

    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    vault, _, authority, bearer, _ = upload_env
    data = b"recording"
    digest = hashlib.sha256(data).hexdigest()
    handoff = _prepare(upload_env, data, size_bytes=len(data))
    monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")

    assert asyncio.run(authority.validate(bearer)) is not None
    with pytest.raises(ValueError, match="UPLOAD_HANDOFF_UNAVAILABLE"):
        asyncio.run(upload_handoff.claim_upload(
            vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
        ))


def test_resumable_ticket_survives_an_unreadable_login_store(upload_env, monkeypatch):
    from exomem import upload_handoff

    monkeypatch.setenv("EXOMEM_UPLOAD_MAX_BYTES", "8")
    vault, _, authority, _, _ = upload_env
    data = b"recording"
    digest = hashlib.sha256(data).hexdigest()
    handoff = _prepare(upload_env, data, size_bytes=len(data))

    async def unavailable(**_):
        raise auth_sessions.SessionStoreUnavailable("temporary read failure")

    with monkeypatch.context() as offline:
        offline.setattr(authority, "revalidate_login", unavailable)
        with pytest.raises(upload_handoff.SourceOriginBusy, match="UPLOAD_ORIGIN_BUSY"):
            asyncio.run(upload_handoff.claim_upload(
                vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
            ))
    recovered = asyncio.run(upload_handoff.claim_upload(
        vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
    ))
    assert recovered["size_bytes"] == len(data)


def test_resumable_ticket_uses_shared_login_when_optional_cache_cannot_open(vault, tmp_path, monkeypatch):
    from exomem import upload_handoff
    from exomem.lease_coordinator import create_app

    cache_parent = tmp_path / "cache-parent"
    cache_parent.write_text("an existing file")
    for key, value in {
        "EXOMEM_BASE_URL": "https://capture.example",
        "EXOMEM_GITHUB_USER_ID": "42",
        "EXOMEM_OWNER_OAUTH_SUBJECT": "github:42",
        "EXOMEM_JWT_SIGNING_KEY": "test-signing-key",
        "EXOMEM_UPLOAD_MAX_BYTES": "8",
        "EXOMEM_OAUTH_STORAGE_URL": "https://coordinator.example",
        "EXOMEM_OAUTH_STORAGE_NAMESPACE": "fixture",
        "EXOMEM_OAUTH_STORAGE_TOKEN": "test-coordinator-token",
        "EXOMEM_SESSION_STALE_GRACE_SECONDS": "3600",
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(cache_parent),
    }.items():
        monkeypatch.setenv(key, value)
    transport = httpx.ASGITransport(app=create_app(
        database=tmp_path / "coord.sqlite", bearer_token="test-coordinator-token",
    ))
    build_remote = auth_sessions.SessionAuthority.remote
    monkeypatch.setattr(auth_sessions.SessionAuthority, "remote", staticmethod(
        lambda **settings: build_remote(transport=transport, **settings),
    ))
    with pytest.raises(FileExistsError):
        server_auth.build_session_authority(base_url="https://capture.example")
    authority = server_auth.build_session_authority(base_url="https://capture.example", authoritative_only=True)
    _, record = asyncio.run(authority.issue(
        client_id="fixture", scopes=["mcp"],
        identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
    ))
    who = principal.RequestPrincipal(
        audience_id="owner", surface="mcp", issuer_family="mcp-oauth:fixture", remote_owner=True,
        client_binding=principal.ClientBinding(record.issuer, record.client_id),
        origin_session=principal.OriginSessionBinding(record.session_id, record.generation, record.audience),
    )
    data = b"recording"
    digest = hashlib.sha256(data).hexdigest()
    with principal.request_scope(who):
        handoff = upload_handoff.prepare_upload(vault, lane="source", expected_sha256=digest, size_bytes=len(data))
    claimed = asyncio.run(upload_handoff.claim_upload(
        vault, handoff["token"], resumable=True, size_bytes=len(data), sha256=digest,
    ))
    assert claimed["principal"] == who


def test_attach_resumes_the_large_source_and_returns_one_verified_receipt(
    upload_env, tmp_path, monkeypatch, capsys,
):
    from exomem import upload_handoff, upload_sessions

    monkeypatch.setattr(upload_handoff, "MAX_BYTES", 8)
    monkeypatch.setattr(upload_sessions, "MAX_PATCH_BYTES", 16)
    data = b"A complete recording arrives in many parts."
    descriptor = _prepare(upload_env, data, size_bytes=len(data))
    handoff = tmp_path / "handoff.json"
    handoff.write_text(json.dumps(descriptor))
    original = tmp_path / "recording.bin"
    original.write_bytes(data)

    class InterruptedPart(_ASGISyncTransport):
        interrupted = False
        resumed = False

        def handle_request(self, request):
            if request.method != "POST":
                assert "authorization" not in request.headers
            response = super().handle_request(request)
            if request.method == "PATCH" and response.status_code == 204 and not self.interrupted:
                self.interrupted = True
                raise httpx.ReadError("the part acknowledgement was lost", request=request)
            if request.method == "HEAD":
                assert response.headers["upload-offset"] == "8"
                self.resumed = True
            return response

    transport = InterruptedPart(upload_env[-1])
    assert _attach_main(["--handoff-file", str(handoff), "--root", str(tmp_path), str(original)],
                        transport=transport) == 0
    assert transport.interrupted and transport.resumed
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["hash"] == hashlib.sha256(data).hexdigest()
    assert receipt["size"] == len(data)
    assert (upload_env[0] / receipt["artifact_path"]).read_bytes() == data
    assert len(list((upload_env[0] / receipt["artifact_path"]).parent.glob("*.bin"))) == 1
    # The same invocation can recover the receipt if its first stdout consumer missed it.
    assert _attach_main(
        ["--handoff-file", str(handoff), "--root", str(tmp_path), str(original)],
        transport=_ASGISyncTransport(upload_env[-1]),
    ) == 0
    assert json.loads(capsys.readouterr().out) == receipt


@pytest.mark.parametrize("revoke_at", [None, "restart", "staging", "owner_binding", "publication"])
def test_a_complete_source_part_recovers_under_its_issuing_login(upload_env, monkeypatch, revoke_at):
    from exomem import upload_handoff, upload_sessions, upload_stage

    monkeypatch.setattr(upload_handoff, "MAX_BYTES", 8)
    data = b"Durable bytes before the state can change."
    digest = hashlib.sha256(data).hexdigest()
    descriptor = _prepare(upload_env, data, size_bytes=len(data))
    vault, _, authority, bearer, app = upload_env
    client = httpx.Client(transport=_ASGISyncTransport(app), base_url="https://capture.example")
    content = {"filename": "recording.bin", "title": "Resumed recording", "sha256": digest}
    open_headers = {
        "Tus-Resumable": "1.0.0",
        "Authorization": "Bearer " + descriptor["token"],
        "Upload-Length": str(len(data)),
        "Upload-Metadata": ",".join(f"{key} {base64.b64encode(value.encode()).decode()}"
                                    for key, value in content.items()),
    }
    wrong_size = {**open_headers, "Upload-Length": str(len(data) + 1)}
    assert client.post(upload_sessions.SESSIONS_PATH, headers=wrong_size).status_code == 401
    invalid_flags = {**open_headers, "Upload-Metadata": open_headers["Upload-Metadata"] + ",raw_protection bm90LWEtZmxhZw=="}
    refused = client.post(upload_sessions.SESSIONS_PATH, headers=invalid_flags)
    assert refused.status_code == 400 and "handoff_spent" not in refused.json()
    assert _post(upload_env, descriptor, data).status_code != 201
    created = client.post(upload_sessions.SESSIONS_PATH, headers=open_headers)
    assert created.status_code == 201, created.text
    assert client.post(upload_sessions.SESSIONS_PATH, headers=open_headers).status_code == 401
    location = created.headers["location"]
    session_id = location.rsplit("/", 1)[1]
    secret = created.headers[upload_sessions.SECRET_HEADER]
    headers = {"Tus-Resumable": "1.0.0", upload_sessions.SECRET_HEADER: secret}

    write_record = upload_sessions._write_record

    def stop_after_part(store, record):
        if record["state"] == upload_sessions.VERIFYING:
            raise OSError("listener stopped before writing the verifying record")
        return write_record(store, record)

    with monkeypatch.context() as stopped:
        stopped.setattr(upload_sessions, "_write_record", stop_after_part)
        with pytest.raises(OSError, match="listener stopped"):
            client.patch(location, headers={**headers, "Upload-Offset": "0",
                         "Content-Type": "application/offset+octet-stream"}, content=data)
    # A fresh serving runtime owns the durable part and record, without the old digest or request context.
    monkeypatch.setattr(upload_sessions, "_RUNNING", {})
    if revoke_at == "restart":
        asyncio.run(authority.revoke_bearer(bearer, reason="test"))
    if revoke_at == "owner_binding":
        monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT")
        assert asyncio.run(authority.validate(bearer)) is not None
    if revoke_at == "staging":
        stage = upload_stage.stage

        @contextmanager
        def revoke_after_staging(*args, **kwargs):
            with stage(*args, **kwargs) as staged:
                asyncio.run(authority.revoke_bearer(bearer, reason="test"))
                yield staged

        monkeypatch.setattr(upload_stage, "stage", revoke_after_staging)
    if revoke_at == "publication":
        from exomem import vault as vault_module

        after = vault_module._after_batch_destination_published
        revoked = False

        def revoke_after_original_is_installed(path):
            nonlocal revoked
            after(path)
            if not revoked and path.suffix == ".bin":
                assert path.read_bytes() == data
                revoked = True
                asyncio.run(authority.revoke_bearer(bearer, reason="test"))

        monkeypatch.setattr(vault_module, "_after_batch_destination_published", revoke_after_original_is_installed)
    server_transfer.resume_upload_sessions(vault)
    deadline = time.monotonic() + 20
    while True:
        state = upload_sessions.open_session(vault, session_id, secret).view()
        if state["state"] in (upload_sessions.COMMITTED, upload_sessions.FAILED):
            break
        assert time.monotonic() < deadline, state
        time.sleep(0.03)
    response = client.get(location, headers=headers)
    if revoke_at is not None:
        if revoke_at == "publication":
            assert revoked
        assert response.status_code == 404
        assert state["state"] == upload_sessions.FAILED
        assert not list(vault.rglob("*.bin"))
    else:
        assert response.status_code == 200
        assert state["state"] == upload_sessions.COMMITTED
        assert response.json()["receipt"]["hash"] == digest
        assert (vault / response.json()["receipt"]["artifact_path"]).read_bytes() == data
        assert len(list(vault.rglob("*.bin"))) == 1


def test_cli_accepts_the_result_of_a_handoff_issued_after_a_write(upload_env, tmp_path):
    # Once a write has synced the graph, every later write result also carries graph_sync.
    descriptor = {**_prepare(upload_env), "graph_sync": "completed"}
    (tmp_path / "handoff.json").write_text(json.dumps(descriptor))
    (tmp_path / "original.bin").write_bytes(b"exact original\n")
    assert _attach_main(
        ["--handoff-file", str(tmp_path / "handoff.json"), "--root", str(tmp_path), "original.bin"],
        transport=_ASGISyncTransport(upload_env[-1]),
    ) == 0


def test_revoked_expired_and_foreign_tickets_share_one_refusal(upload_env, monkeypatch):
    from exomem import upload_handoff
    revoked = _prepare(upload_env)
    expired = _prepare(upload_env)
    asyncio.run(upload_env[2].revoke_bearer(upload_env[3], reason="test"))
    refused = _post(upload_env, revoked)
    assert refused.status_code == 401
    monkeypatch.setattr(upload_handoff, "_now", lambda: 10**12)
    timed_out = _post(upload_env, expired)
    foreign = _post(upload_env, {"token": upload_handoff.TOKEN_PREFIX + "0" * 64})
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


def test_ticket_consumption_must_be_durable_before_the_body_is_accepted(upload_env, monkeypatch):
    """A crash can replay an unflushed ticket and recreate a cancelled upload's identity."""
    from exomem import private_state

    descriptor = _prepare(upload_env)
    body_read = False

    def unavailable_directory(_store):
        raise OSError("directory durability unavailable")

    monkeypatch.setattr(private_state, "sync_directory", unavailable_directory)

    async def send():
        async def chunks():
            nonlocal body_read
            body_read = True
            yield b"an upload body that must never be accepted"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=upload_env[-1]), base_url="https://capture.example",
        ) as client:
            return await client.post(
                "/upload", headers={"Authorization": "Bearer " + descriptor["token"],
                    "Content-Type": "multipart/form-data; boundary=unread"}, content=chunks(),
            )

    assert asyncio.run(send()).status_code == 401
    assert body_read is False
    assert asyncio.run(send()).status_code == 401
    assert body_read is False


def test_cancelling_a_restored_hold_releases_only_its_session_quota(upload_env):
    """A failed preservation's hold can fill the cap until its verified session cancels it."""
    vault, who, *_ = upload_env
    data = b"original"
    response = _post(upload_env, _prepare(upload_env, data, max_bytes=len(data)), data)
    assert response.status_code == 201
    reference = response.json()["file"]["download_url"]
    foreign = replace(who, origin_session=replace(who.origin_session, session_id="other"))
    with principal.request_scope(foreign):
        with pytest.raises(held_uploads.HeldUploadError) as denied:
            held_uploads._cancel(vault, reference)
        with pytest.raises(held_uploads.HeldUploadError) as missing:
            held_uploads._cancel(vault, held_uploads.HELD_SCHEME + "x" * 43)
        assert (denied.value.code, denied.value.reason) == (missing.value.code, missing.value.reason)
    with principal.request_scope(who):
        claimed = held_uploads.redeem(vault, reference, lane="source")
        with pytest.raises(held_uploads.HeldUploadError, match="no redeemable"):
            held_uploads._cancel(vault, reference)
        assert held_uploads.restore(vault, claimed)
        with pytest.raises(held_uploads.HeldUploadError, match="too many uploads"):
            held_uploads.hold(vault, BytesIO(data), lane="source", filename="next.txt",
                              content_type="text/plain", max_bytes=len(data))
        assert held_uploads._cancel(vault, reference) == len(data)
        with pytest.raises(held_uploads.HeldUploadError, match="no redeemable"):
            held_uploads.redeem(vault, reference, lane="source")
        with pytest.raises(held_uploads.HeldUploadError, match="no redeemable"):
            held_uploads._cancel(vault, reference)
    assert _post(upload_env, _prepare(upload_env, data, max_bytes=len(data)), data).status_code == 201


def test_failed_cancellation_keeps_payload_ownership_and_quota_beyond_expiry(upload_env, monkeypatch):
    """Expiry must not discard the only cleanup record after payload removal fails."""
    from pathlib import Path

    vault, who, *_ = upload_env
    data = b"original"
    held = _post(upload_env, _prepare(upload_env, data, max_bytes=len(data)), data).json()
    reference = held["file"]["download_url"]
    store = held_uploads._store(vault)
    payload, = store.glob("*.bin")
    original_unlink = Path.unlink

    def blocked_payload(path, *args, **kwargs):
        if path == payload:
            raise OSError("payload removal unavailable")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_payload)
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as failed:
            held_uploads._cancel(vault, reference)
        assert failed.value.code == "HELD_UPLOAD_CANCELLATION_INCOMPLETE"
        marker, = store.glob("*.cancelling.json")
        expires = json.loads(marker.read_text())["expires"]
        monkeypatch.setattr(held_uploads, "_now", lambda: expires + 1)
        held_uploads._sweep(store, expires + 1)
        assert marker.exists() and payload.read_bytes() == data
        with pytest.raises(held_uploads.HeldUploadError, match="no redeemable"):
            held_uploads.hold(vault, BytesIO(b"replacement"), lane="source", filename="next.txt",
                              content_type="text/plain", max_bytes=32,
                              secret=reference[len(held_uploads.HELD_SCHEME):])
        assert marker.exists() and payload.read_bytes() == data
        with pytest.raises(held_uploads.HeldUploadError, match="too many uploads"):
            held_uploads.hold(vault, BytesIO(data), lane="source", filename="next.txt",
                              content_type="text/plain", max_bytes=len(data))
        monkeypatch.setattr(Path, "unlink", original_unlink)
        assert held_uploads._cancel(vault, reference) == len(data)
        assert not marker.exists() and not payload.exists()


def test_unconfirmed_cancellation_rename_leaves_payload_for_retry(upload_env, monkeypatch):
    """A failed directory flush must not delete bytes whose cleanup ownership could disappear."""
    from exomem import private_state

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    reference = held["file"]["download_url"]
    store = held_uploads._store(vault)
    payload, = store.glob("*.bin")
    original_sync = private_state.sync_directory

    def blocked_directory(_store):
        raise OSError("directory durability unavailable")

    monkeypatch.setattr(private_state, "sync_directory", blocked_directory)
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as failed:
            held_uploads._cancel(vault, reference)
        assert failed.value.code == "HELD_UPLOAD_CANCELLATION_INCOMPLETE"
        assert payload.read_bytes() == b"exact original\n"
        assert len(list(store.glob("*.cancelling.json"))) == 1
        monkeypatch.setattr(private_state, "sync_directory", original_sync)
        assert held_uploads._cancel(vault, reference) == held["size"]


def test_cancellation_retries_after_payload_is_gone_but_marker_removal_fails(upload_env, monkeypatch):
    """A partial cleanup must retain its quota owner and finish without reading the missing payload."""
    from pathlib import Path

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    reference = held["file"]["download_url"]
    store = held_uploads._store(vault)
    original_unlink = Path.unlink

    def blocked_marker(path, *args, **kwargs):
        if path.name.endswith(".cancelling.json"):
            raise OSError("cleanup record removal unavailable")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_marker)
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as failed:
            held_uploads._cancel(vault, reference)
        assert failed.value.code == "HELD_UPLOAD_CANCELLATION_INCOMPLETE"
        assert not list(store.glob("*.bin"))
        assert len(list(store.glob("*.cancelling.json"))) == 1
        monkeypatch.setattr(Path, "unlink", original_unlink)
        assert held_uploads._cancel(vault, reference) == held["size"]


def test_cancellation_returns_busy_without_consuming_a_hold(upload_env):
    """A competing claim must not leave cancellation waiting behind upload work."""
    from filelock import FileLock

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    reference = held["file"]["download_url"]
    with principal.request_scope(who):
        with FileLock(held_uploads._store(vault) / ".lock"):
            with pytest.raises(held_uploads.HeldUploadError) as busy:
                held_uploads._cancel(vault, reference)
            assert busy.value.code == "HELD_UPLOAD_BUSY"
        assert held_uploads._cancel(vault, reference) == held["size"]


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
    assert response.json()["reason"] == upload_handoff.TICKET_UNUSABLE_REASON


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


def test_a_worker_whose_launcher_declared_nothing_cannot_activate_cancellation(upload_env, monkeypatch):
    """A new worker under an old launcher must keep held bytes redeemable."""
    from exomem import artifact_lifecycle, state_migration

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    artifact_lifecycle.claim_launcher_support(None)  # an old launcher passes no lifecycle argument
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as refused:
            held_uploads.cancel(vault, held["file"]["download_url"])
        assert refused.value.code == "HELD_UPLOAD_LIFECYCLE_REQUIRED"
        claimed = held_uploads.redeem(vault, held["file"]["download_url"], lane="source")
        assert claimed.path.read_bytes() == b"exact original\n"
        assert held_uploads.restore(vault, claimed)
    assert not list(held_uploads._store(vault).glob("*.cancelling.json"))
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)


def test_a_newer_launchers_unknown_lifecycle_formats_do_not_stop_this_worker(monkeypatch):
    """A later launcher declares formats this release never wrote; a downgrade must still start."""
    from exomem import artifact_lifecycle, state_migration

    cancellation = state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    artifact_lifecycle.claim_launcher_support([cancellation, "a-later-format-v1"])
    assert artifact_lifecycle.supports(cancellation)
    assert not artifact_lifecycle.supports("a-later-format-v1")


def test_unknown_and_foreign_handles_never_record_the_cancellation_format(upload_env, monkeypatch):
    """Only proven ownership may add a step to every later downgrade."""
    from exomem import artifact_lifecycle, state_migration

    vault, who, *_ = upload_env
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as unknown:
            held_uploads.cancel(vault, held_uploads.HELD_SCHEME + "x" * 43)
    assert unknown.value.code == "HELD_UPLOAD_UNAVAILABLE"
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in (
        state_migration.recorded_descriptor_ids(vault) or ()
    )


def test_a_url_in_a_cancellation_batch_names_the_handle_and_cancels_nothing(upload_env, monkeypatch):
    """An agent that passes the attachment URL learns what to send instead; its real hold survives."""
    from exomem import artifact_lifecycle, commands, state_migration

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    with principal.request_scope(who):
        with pytest.raises(ValueError, match=r"uploads\[1\].*exomem-held:"):
            commands.op_transfer_artifact(
                vault, operation="cancel",
                uploads=[held["file"]["download_url"], "https://files.example/report.pdf"],
            )
        claimed = held_uploads.redeem(vault, held["file"]["download_url"], lane="source")
        assert held_uploads.restore(vault, claimed)
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in (
        state_migration.recorded_descriptor_ids(vault) or ()
    )


def test_a_read_only_cloud_cell_refuses_cancellation_before_deleting_bytes(upload_env, monkeypatch):
    """Read-only mode refuses every mutation, including writes outside canonical writer custody."""
    from exomem import artifact_lifecycle, commands, writer_lease
    from exomem.cli_ops import OpError

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    monkeypatch.setenv("EXOMEM_CLOUD_READ_ONLY", "1")
    command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "transfer_artifact")
    with principal.request_scope(who):
        with pytest.raises(OpError) as refused:
            writer_lease.invoke_command(command, vault, operation="cancel", uploads=[held["file"]["download_url"]])
        assert refused.value.code == "CLOUD_CELL_READ_ONLY"
        claimed = held_uploads.redeem(vault, held["file"]["download_url"], lane="source")
        assert claimed.path.read_bytes() == b"exact original\n"
        assert held_uploads.restore(vault, claimed)


@pytest.mark.parametrize("managed_worker", [False, True])
def test_only_a_standalone_server_is_its_own_cancellation_launcher(monkeypatch, tmp_path, managed_worker):
    """Cloud cells and foreground servers can cancel; a managed worker still needs its launcher's claim."""
    from exomem import artifact_lifecycle, cuda_containment, logging_config, server, state_migration

    class Started(Exception):
        pass

    def stop(*args, **kwargs):
        raise Started

    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset())
    monkeypatch.setattr(cuda_containment, "hide_in_process", lambda: None)
    monkeypatch.setattr(logging_config, "configure_logging", stop)
    with pytest.raises(Started):
        server.run(transport="streamable-http", worker_socket=tmp_path / "worker.sock" if managed_worker else None)
    cancellation = state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID
    assert artifact_lifecycle.supports(cancellation) is not managed_worker


def test_cancellation_activation_flushes_consumed_tickets_before_enrollment(upload_env, monkeypatch):
    """A failed activation barrier must leave the upload and its previous format intact."""
    from exomem import artifact_lifecycle, private_state, state_migration, upload_handoff

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    original_sync = private_state.sync_directory
    tickets = private_state.store(vault, upload_handoff.STORE_DIRNAME)

    def unavailable_barrier(directory):
        if directory == tickets:
            raise OSError("ticket consumption is not durable")
        return original_sync(directory)

    monkeypatch.setattr(private_state, "sync_directory", unavailable_barrier)
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError) as failed:
            held_uploads.cancel(vault, held["file"]["download_url"])
        assert failed.value.code == "HELD_UPLOAD_CANCELLATION_INCOMPLETE"
        assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)
        claimed = held_uploads.redeem(vault, held["file"]["download_url"], lane="source")
        assert held_uploads.restore(vault, claimed)
        monkeypatch.setattr(private_state, "sync_directory", original_sync)
        assert held_uploads.cancel(vault, held["file"]["download_url"]) == held["size"]
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)


def test_offline_cancellation_cleanup_can_resume_without_retiring_other_formats(upload_env, monkeypatch, capsys):
    """An interrupted downgrade must keep its cleanup requirement and every other recorded format."""
    import time
    from pathlib import Path

    from exomem import artifact_lifecycle, state_migration

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    state_migration.record_collections_sqlite_only(vault, authority_check=lambda: True)
    original_unlink = Path.unlink

    def blocked_payload(path, *args, **kwargs):
        if path.parent.name == held_uploads.STORE_DIRNAME and path.suffix == ".bin":
            raise OSError("payload removal unavailable")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_payload)
    with principal.request_scope(who):
        with pytest.raises(held_uploads.HeldUploadError):
            held_uploads.cancel(vault, held["file"]["download_url"])
    authority = state_migration.assert_offline_migration_authority(source="owned stopped test service")
    with pytest.raises(OSError, match="payload removal"):
        artifact_lifecycle.cleanup_cancellations(vault, authority=authority, deadline=time.monotonic()+10)
    required = state_migration.recorded_descriptor_ids(vault)
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID in required
    assert state_migration.COLLECTIONS_SQLITE_ONLY_COMPATIBILITY_ID in required
    monkeypatch.setattr(Path, "unlink", original_unlink)
    from exomem.__main__ import _simple_maintain_main

    assert _simple_maintain_main([
        "--vault", str(vault), "--migrate-state", "--offline",
        "--cleanup-held-upload-cancellations", "--deadline-monotonic", str(time.monotonic()+10), "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"success": True, "data": {"completed": 1}}
    required = state_migration.recorded_descriptor_ids(vault)
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in required
    assert state_migration.COLLECTIONS_SQLITE_ONLY_COMPATIBILITY_ID in required
    assert not list(held_uploads._store(vault).glob("*.bin"))
    assert not list(held_uploads._store(vault).glob("*.cancelling.json"))


def test_an_operator_can_finish_cancellations_without_a_launcher_deadline(upload_env, monkeypatch, capsys):
    """A standalone or Cloud downgrade needs the cleanup command a person can type."""
    from exomem import artifact_lifecycle, state_migration
    from exomem.__main__ import _simple_maintain_main

    vault, who, *_ = upload_env
    held = _post(upload_env, _prepare(upload_env)).json()
    monkeypatch.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    monkeypatch.setattr(artifact_lifecycle, "_activated", set())
    with principal.request_scope(who):
        assert held_uploads.cancel(vault, held["file"]["download_url"]) == held["size"]
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID in state_migration.recorded_descriptor_ids(vault)
    assert _simple_maintain_main([
        "--vault", str(vault), "--migrate-state", "--offline", "--cleanup-held-upload-cancellations", "--json",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == {"success": True, "data": {"completed": 0}}
    assert state_migration.HELD_UPLOAD_CANCELLATION_COMPATIBILITY_ID not in state_migration.recorded_descriptor_ids(vault)


def test_a_source_reply_names_the_send_that_holds_the_file(upload_env, tmp_path, monkeypatch):
    # An agent that ran a send without the ticket's form fields got a 400 and lost the ticket.
    from fastmcp import Client

    from exomem import server
    _, who, *_, receiver = upload_env
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: who)
    original = tmp_path / "original.txt"
    original.write_bytes(b"exact original\n")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()

    async def run():
        async with Client(server.build_server(require_auth=False)) as client:
            reply = (await client.call_tool(
                "upload_files", {"lane": "source", "expected_sha256": digest})).structured_content
            send, save = reply["next"]
            url, request = curl_request(send["shell"], token=reply["token"], file=str(original))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver)) as http:
                sent = await http.post(url, **request)
            assert sent.status_code == 201, sent.text
            # The save step names a tool that this surface lists.
            saved = await client.call_tool(save["tool"], {
                "title": "Exact original", "source_kind": "session", "files": [sent.json()["file"]]})
            return saved.structured_content

    [receipt] = asyncio.run(run())["files"]
    assert receipt["outcome"] == "stored" and receipt["hash"] == digest


def test_a_refused_send_says_a_retry_needs_a_new_upload(upload_env):
    # The claim spends the ticket, so an agent that retries it unaided is refused again with no way on.
    from exomem import upload_handoff
    descriptor = _prepare(upload_env)
    refused = _post(upload_env, descriptor, lane="")
    assert refused.status_code == 400
    assert "lane=source" in refused.json()["reason"]
    assert upload_handoff.SPENT_REMEDY in refused.json()["reason"]
    retried = _post(upload_env, descriptor)
    assert retried.status_code == 401
    assert retried.json()["reason"] == upload_handoff.TICKET_UNUSABLE_REASON


def test_import_id_survives_a_new_hosted_upload_and_stays_scoped_to_the_importer(
    upload_env, monkeypatch,
):
    from fastmcp import Client

    from exomem import server

    vault, who, authority, _, _ = upload_env
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: active)
    original = b"[0:09] Gate opened.\r\n[0:14] Gate shut.\r\n"
    first_bytes = hashlib.sha256(original).hexdigest()
    active = who
    app = server.build_server(require_auth=False)

    def caller(client_id):
        _, record = asyncio.run(authority.issue(
            client_id=client_id, scopes=["mcp"],
            identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="example"),
        ))
        return replace(
            who, client_binding=principal.ClientBinding(authority.issuer, record.client_id),
            origin_session=principal.OriginSessionBinding(record.session_id, record.generation, record.audience),
        )

    def capture(data, import_identity="meeting-42"):
        descriptor = _prepare(
            (vault, active, *upload_env[2:]), data, import_identity=import_identity,
        )
        sent = _post(upload_env, descriptor, data)
        assert sent.status_code == 201, sent.text

        async def invoke():
            async with Client(app) as client:
                return (await client.call_tool("save_source", {
                    "title": "Gate log", "source_kind": "session", "files": [sent.json()["file"]],
                    "detail": "full",
                })).structured_content

        result = asyncio.run(invoke())
        [receipt] = result["files"]
        return receipt

    first = capture(original)
    assert first["hash"] == first_bytes
    assert first["outcome"] == "stored"
    active = caller("agent")
    repeated = capture(original)
    assert repeated["ref"] == first["ref"] and repeated["hash"] == first_bytes
    assert repeated["state"] == "already_stored"
    conflict = capture(b"different file\r\n")
    assert conflict["code"] == "IMPORT_IDENTITY_CONFLICT"
    from exomem import lexstore

    lexstore.get_store(vault).rebuild_atomic()
    reused = capture(original, import_identity="same-meeting-in-a-second-feed")
    assert reused["ref"] == first["ref"] and reused["state"] == "already_stored"
    replayed_reuse = capture(original, import_identity="same-meeting-in-a-second-feed")
    assert replayed_reuse["ref"] == first["ref"] and replayed_reuse["state"] == "already_stored"
    active = caller("different-agent")
    second = capture(b"different file\r\n")
    assert second["outcome"] == "stored" and second["ref"] != first["ref"]

    from exomem import source_custody

    # This duplicate has a managed erasure obligation. A new import ID must
    # leave no durable pointer to it, while a different file can bind that ID.
    original_read_manifest = source_custody.read_manifest
    monkeypatch.setattr(
        source_custody, "read_manifest",
        lambda root, ref: (object(), None) if ref == first["ref"] else original_read_manifest(root, ref),
    )
    lexstore.get_store(vault).rebuild_atomic()
    retirement_bound = capture(original, import_identity="retirement-check")
    assert retirement_bound["code"] == "IMPORT_RECEIPT_UNAVAILABLE"
    available = capture(b"another file\r\n", import_identity="retirement-check")
    assert available["outcome"] == "stored"


def test_import_id_refuses_a_container_with_a_normalized_extension(upload_env, source_schema):
    from test_document_containers import container_bytes

    from exomem import commands

    vault, who, *_ = upload_env
    data = container_bytes([("note.txt", b"Original document.")])
    handoff = _prepare(upload_env, data, import_identity="example-container")
    uploaded = _post(upload_env, handoff, data, filename="record.asi?ce")
    assert uploaded.status_code == 201, uploaded.text

    with principal.request_scope(who):
        result = commands.op_capture_source(
            vault, source_schema=source_schema, title="Example container", source_kind="session",
            files=[uploaded.json()["file"]],
        )

    assert result["files"][0]["code"] == "IMPORT_IDENTITY_COMPOUND_SOURCE"
    assert result["summary"] == {"stored": 0, "failed": 1}


def test_import_retry_completes_the_original_after_source_publication_stops(
    upload_env, source_schema, monkeypatch,
):
    from exomem import commands
    from exomem import vault as vault_module

    vault, who, *_ = upload_env
    original = b"Record bytes\x00\xff"
    source_paths = []

    class Interrupted(BaseException):
        pass

    def stop_at_source(path):
        if path.name.endswith(".txt.md") and path.is_relative_to(vault) and (
            vault_module.in_append_only_tree(path.relative_to(vault).as_posix()) == "Sources"
        ):
            source_paths.append(path)
            raise Interrupted()

    def invoke():
        handoff = _prepare(upload_env, original, import_identity="repeatable-file")
        uploaded = _post(upload_env, handoff, original)
        assert uploaded.status_code == 201, uploaded.text
        with principal.request_scope(who):
            return commands.op_capture_source(
                vault, source_schema=source_schema, title="Gate district records", source_kind="session",
                files=[uploaded.json()["file"]],
            )

    with monkeypatch.context() as patch:
        patch.setattr(vault_module, "_after_batch_destination_published", stop_at_source)
        with pytest.raises(Interrupted):
            invoke()
    [source] = source_paths
    before, text, _ = vault_module.parse_frontmatter(source.read_text(encoding="utf-8"))
    target = source.with_suffix("")
    assert not target.exists()

    [receipt] = invoke()["files"]
    assert receipt["state"] == "already_stored"
    assert receipt["hash"] == hashlib.sha256(original).hexdigest()
    after, recovered_text, _ = vault_module.parse_frontmatter(source.read_text(encoding="utf-8"))
    assert recovered_text == text
    assert after["evidence_file"] == before["evidence_file"]
    assert after["exomem_id"] == before["exomem_id"]
    assert target.read_bytes() == original
    assert invoke()["files"][0]["ref"] == receipt["ref"]


def test_concurrent_held_uploads_with_the_same_import_id_file_once(upload_env, source_schema):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from exomem import commands

    vault, who, *_ = upload_env
    original = b"The gate survey is complete.\r\n"
    uploads = []
    for _ in range(2):
        handoff = _prepare(upload_env, original, import_identity="parallel-survey")
        uploaded = _post(upload_env, handoff, original)
        assert uploaded.status_code == 201, uploaded.text
        uploads.append(uploaded.json()["file"])
    ready = Barrier(2)

    def save(file):
        with principal.request_scope(who):
            ready.wait(timeout=10)
            return commands.op_capture_source(
                vault, source_schema=source_schema, title="Gate survey", source_kind="session", files=[file],
            )["files"][0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(save, uploads)

    assert first["ref"] == second["ref"]
    assert first["hash"] == second["hash"] == hashlib.sha256(original).hexdigest()
    assert sum(row.get("state") == "already_stored" for row in (first, second)) == 1

    from exomem import note
    from exomem import vault as vault_module

    page = vault / first["page"]
    frontmatter, body, _ = vault_module.parse_frontmatter(page.read_text(encoding="utf-8"))
    with principal.request_scope(who):
        summary = note.note(
            vault, title="Gate survey summary", note_type="insight", status="draft",
            content="## Findings\n\n- [finding] The survey team completed its inspection.",
            sources=[first["page"]],
        )
    updated, updated_body, _ = vault_module.parse_frontmatter(page.read_text(encoding="utf-8"))
    assert updated_body == body
    assert updated["evidence_file"] == frontmatter["evidence_file"]
    assert (vault / updated["evidence_file"]).read_bytes() == original
    assert summary.path.removesuffix(".md") in str(updated["ingested_into"])
