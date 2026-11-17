"""Agent-local bytes reach a session-bound hold without a second credential."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
