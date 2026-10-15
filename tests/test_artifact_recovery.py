"""Exact original recovery uses retained files and current release authority."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

from exomem import reserved_paths


def test_retained_copy_rechecks_bytes_even_with_unchanged_mtime(vault, tmp_path):
    original = vault / "sample.bin"
    original.write_bytes(b"first original")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied = reserved_paths.copy_generic_file(vault, "sample.bin", private / "original")
    assert copied.sha256 == hashlib.sha256(b"first original").hexdigest()
    assert copied.size == len(b"first original")
    reserved_paths.require_current_generic_copy(copied)
    stat = original.stat()
    original.write_bytes(b"other original")
    os.utime(original, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        reserved_paths.require_current_generic_copy(copied)


@pytest.mark.parametrize("alias", ["hardlink", "symlink", "replaced_parent"])
def test_retained_copy_refuses_canonical_alias_changes(vault, tmp_path, alias):
    directory = vault / "copy-records"
    directory.mkdir()
    original = directory / "sample.bin"
    original.write_bytes(b"original")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied = reserved_paths.copy_generic_file(
        vault, "copy-records/sample.bin", private / "original"
    )
    if alias == "hardlink":
        os.link(original, directory / "alias.bin")
    elif alias == "symlink":
        original.unlink()
        original.symlink_to(private / "original")
    else:
        directory.rename(vault / "previous")
        directory.mkdir()
        (directory / "sample.bin").write_bytes(b"original")
    with pytest.raises(reserved_paths.ReservedPathLeafError):
        reserved_paths.require_current_generic_copy(copied)


def test_browser_recovers_preserved_original_after_external_removal(vault, monkeypatch, tmp_path):
    from test_upload_endpoint import _client

    from exomem import artifact_recovery, commands
    from exomem.governance.principal import owner_principal, request_scope

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    client = _client(
        vault,
        monkeypatch,
        EXOMEM_UPLOAD_TOKEN="transport-secret",
        EXOMEM_JWT_SIGNING_KEY="private-authority",
        EXOMEM_BASE_URL="https://archive.example",
    )
    external = tmp_path / "external.wav"
    external.write_bytes(b"RIFF" + b"original audio\x00" * 5000)
    original = external.read_bytes()
    result = client.post(
        "/upload",
        files={"file": ("recording.wav", original, "audio/wav")},
        data={"scope": "Archive", "category": "Recordings"},
        headers={"authorization": "Bearer transport-secret"},
    )
    assert result.status_code == 201, result.text
    stored = result.json()
    external.unlink()
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(
            vault,
            operation="download",
            artifact=stored["path"],
            expected_sha256=hashlib.sha256(original).hexdigest(),
        )
    assert handoff["handoff_status"] == "handoff_prepared"
    assert handoff["artifact"]["size"] == len(original)
    assert handoff["artifact"]["mime_type"].startswith("audio/")
    assert handoff["artifact"]["mime_type_source"] == "filename"
    url = handoff["deliveries"]["https"]["url"]
    downloaded = client.get(url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == original
    assert "attachment;" in downloaded.headers["content-disposition"]
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["referrer-policy"] == "no-referrer"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert not list(tmp_path.glob("exomem-recovery-*/private/original"))
    # A derivative is a new preserved object; the caller records where its original came from.
    derivative = client.post(
        "/upload",
        files={"file": ("excerpt.wav", original[:100], "audio/wav")},
        data={
            "scope": "Archive",
            "category": "Derivatives",
            "description": "Derived from " + stored["path"],
        },
        headers={"authorization": "Bearer transport-secret"},
    )
    assert derivative.status_code == 201, derivative.text
    assert derivative.json()["hash"] == hashlib.sha256(original[:100]).hexdigest()
    assert stored["path"] in (vault / derivative.json()["sidecar_path"]).read_text()
    with pytest.raises(artifact_recovery.RecoveryUnavailable):
        with artifact_recovery.stage(
            vault, stored["path"], owner_principal(surface="mcp"), expected_sha256="0" * 64
        ):
            pytest.fail("mismatched version reached delivery")


def test_exact_grants_refuse_cross_delivery_expiry_and_path_override(vault, monkeypatch):
    from test_upload_endpoint import _client

    from exomem import commands, upload_tokens
    from exomem.governance.principal import owner_principal, request_scope

    client = _client(
        vault,
        monkeypatch,
        EXOMEM_UPLOAD_TOKEN="transport-secret",
        EXOMEM_JWT_SIGNING_KEY="private-authority",
        EXOMEM_BASE_URL="https://archive.example",
    )
    stored = client.post(
        "/upload",
        files={"file": ("scan.pdf", b"%PDF-test", "application/pdf")},
        data={"scope": "Archive", "category": "Records"},
        headers={"authorization": "Bearer transport-secret"},
    ).json()
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(
            vault, operation="download", artifact=stored["path"]
        )
    url = handoff["deliveries"]["https"]["url"]
    assert client.get(url + "&path=" + stored["path"]).status_code == 404
    native = handoff["deliveries"]["native_resource"]["uri"].removeprefix("exomem://original/")
    assert client.get("/download", params={"grant": native}).status_code == 404
    broad = upload_tokens.mint_bound("transport-secret", audience="owner")
    assert client.get("/download", params={"grant": broad}).status_code == 404
    assert (
        client.get("/download", params={"path": stored["path"], "token": broad}).status_code == 401
    )
    (vault / stored["path"]).write_bytes(b"%PDF-new!")
    assert client.get(url).status_code == 404
    monkeypatch.setattr(
        upload_tokens.time, "time", lambda: handoff["deliveries"]["https"]["expires_at"]
    )
    denied = client.get(url)
    assert denied.status_code == 404
    assert denied.headers["cache-control"] == "no-store"


def test_native_resource_is_consumed_over_mcp_and_rejects_a_foreign_client(
    vault, monkeypatch, tmp_path
):
    import asyncio
    from dataclasses import replace

    from fastmcp import Client
    from mcp.shared.exceptions import MCPError
    from test_upload_endpoint import _client

    from exomem import artifact_recovery
    from exomem.governance import principal

    http = _client(
        vault,
        monkeypatch,
        EXOMEM_UPLOAD_TOKEN="transport-secret",
        EXOMEM_JWT_SIGNING_KEY="private-authority",
        EXOMEM_BASE_URL="https://archive.example",
    )
    original = b"%PDF-synthetic original"
    stored = http.post(
        "/upload",
        files={"file": ("manual.pdf", original, "application/pdf")},
        data={"scope": "Archive", "category": "Records"},
        headers={"authorization": "Bearer transport-secret"},
    ).json()
    mcp = http.app.state.fastmcp_server
    import fastmcp.server.server as framework

    traced = []
    span = framework.server_span

    def observed_span(*args, **kwargs):
        if kwargs.get("resource_uri"):
            traced.append(kwargs["resource_uri"])
        return span(*args, **kwargs)

    monkeypatch.setattr(framework, "server_span", observed_span)

    async def consume():
        async with Client(mcp) as client:
            handoff = await client.call_tool(
                "transfer_artifact", {"operation": "download", "artifact": stored["path"]}
            )
            payload = handoff.structured_content
            assert payload["artifact"]["sha256"] == hashlib.sha256(original).hexdigest()
            uri = payload["deliveries"]["native_resource"]["uri"]
            content = await client.read_resource(uri)
            import base64

            recovered = base64.b64decode(content[0].blob)
            local = tmp_path / "recovered.pdf"
            local.write_bytes(recovered)
            assert local.read_bytes() == original
            issuer = artifact_recovery.native_principal(
                vault, uri, principal.owner_principal(surface="mcp")
            )
            foreign = replace(
                issuer,
                client_binding=principal.ClientBinding("https://archive.example", "foreign-client"),
            )
            monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: foreign)
            with pytest.raises(MCPError):
                await client.read_resource(uri)

    asyncio.run(consume())
    assert traced == [artifact_recovery.RESOURCE_PREFIX + "authorized"]


def test_copy_policy_matches_bytes_and_companion_edit_invalidates_warm_decision(
    vault, monkeypatch, tmp_path
):
    import yaml

    from exomem import vault as vault_module
    from exomem.governance import companions, egress
    from exomem.governance.principal import RequestPrincipal
    from exomem.preserve import _render_sidecar

    original_path = "Knowledge Base/Evidence/Archive/record.bin"
    original = b"opaque original\x00"
    target = vault / original_path
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    companion = vault / (original_path + ".md")
    companion.write_text(
        _render_sidecar(
            artifact_name="record.bin",
            scope="Archive",
            category="Records",
            date_iso="2026-10-01",
            binary_sha256=hashlib.sha256(original).hexdigest(),
            binary_size=len(original),
            governance_artifact_path=original_path,
            governance_artifact_sha256=hashlib.sha256(original).hexdigest(),
            governance_artifact_size=len(original),
        )
    )
    governance = vault / "Knowledge Base/_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir()
    (governance / "scopes/restricted.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\ntags: [restricted]\n"
    )
    (governance / "rules/restricted.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [01ARZ3NDEKTSV4RRFFQ69G5FAV]\naudience: external\nceiling: 1\n"
    )
    who = RequestPrincipal("external", issuer_family="test-issuer")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied = reserved_paths.copy_generic_file(vault, original_path, private / "original")
    old = companions.classify(vault, original_path)
    assert old == companions.classify(vault, original_path, artifact_copy=copied)
    assert egress.release_allows_download(vault, original_path, principal=who, snapshot=original)
    assert egress.release_allows_download(vault, original_path, principal=who, artifact_copy=copied)
    metadata, body, _ = vault_module.parse_frontmatter(companion.read_text(), strict=True)
    metadata["governance_companion"]["semantics"]["tags"] = ["restricted"]
    companion.write_text("---\n" + yaml.safe_dump(metadata) + "---\n" + body)
    assert not egress.release_allows_download(
        vault, original_path, principal=who, snapshot=original
    )
    read = reserved_paths.read_generic_bytes

    def no_original(root, path, **kwargs):
        assert path != original_path, "disclosure reread the whole original"
        return read(root, path, **kwargs)

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", no_original)
    assert not egress.release_allows_download(
        vault, original_path, principal=who, artifact_copy=copied
    )


def test_scene_frame_uses_bounded_parent_copy_and_keeps_the_same_binding(
    vault, monkeypatch, tmp_path
):
    from exomem.governance import companions
    from exomem.preserve import _render_sidecar

    parent = "Knowledge Base/Evidence/Archive/movie.mp4"
    frame = parent + ".frames/scene-000-t1250ms.jpg"
    (vault / frame).parent.mkdir(parents=True)
    (vault / parent).write_bytes(b"parent original\x00" * 10000)
    original = b"frame original"
    (vault / frame).write_bytes(original)
    parent_digest = hashlib.sha256((vault / parent).read_bytes()).hexdigest()
    (vault / (frame + ".md")).write_text(
        _render_sidecar(
            artifact_name=Path(frame).name,
            scope="Archive",
            category="Frames",
            date_iso="2026-10-01",
            media_type="image",
            evidence_file=frame,
            parent_media=parent,
            frame_ts=1.25,
            binary_sha256=hashlib.sha256(original).hexdigest(),
            binary_size=len(original),
            governance_artifact_path=frame,
            governance_artifact_sha256=hashlib.sha256(original).hexdigest(),
            governance_artifact_size=len(original),
            governance_parent_path=parent,
            governance_parent_sha256=parent_digest,
            governance_frame_timestamp_ms=1250,
        )
    )
    expected = companions.classify(vault, frame)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied = reserved_paths.copy_generic_file(
        vault, frame, private / "original", max_bytes=1024 * 1024
    )
    read = reserved_paths.read_generic_bytes

    def no_original(root, path, **kwargs):
        assert path not in {parent, frame}, "classification reread whole media bytes"
        return read(root, path, **kwargs)

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", no_original)
    read_bytes = Path.read_bytes

    def no_whole_media(path):
        assert path not in {vault / parent, vault / frame}, (
            "classification reread whole media bytes"
        )
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", no_whole_media)
    assert companions.classify(vault, frame, artifact_copy=copied) == expected
    (vault / parent).write_bytes(b"changed parent")
    with pytest.raises(companions.CompanionClassificationError):
        companions.classify(vault, frame, artifact_copy=copied)


def test_retained_copy_budget_stops_growth_and_cleans_partial_destination(
    vault, monkeypatch, tmp_path
):
    from exomem import held_fs

    original = vault / "growing.bin"
    original.write_bytes(b"original")
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with held_fs.acquire(vault).require() as source:
        with source.parent(".").require() as parent:
            with source.file(parent, "growing.bin").require() as file:
                with held_fs.acquire(private).require() as destination:
                    with destination.parent(".").require() as target:
                        result = source.copy(file, target, "copy", max_bytes=3)
                        assert not result.ok
                        result = source.copy(file, target, "copy", deadline=0)
                        assert not result.ok
    assert list(private.iterdir()) == []


@pytest.mark.parametrize("invalidated", ["expiry", "session", "policy"])
def test_authority_changed_during_staging_refuses_before_headers(
    vault, monkeypatch, tmp_path, invalidated
):
    import asyncio

    from test_upload_endpoint import _client

    from exomem import auth_sessions, commands, server_auth, upload_tokens
    from exomem.governance.principal import (
        ClientBinding,
        OriginSessionBinding,
        RequestPrincipal,
        request_scope,
    )

    client = _client(
        vault,
        monkeypatch,
        EXOMEM_UPLOAD_TOKEN="transport-secret",
        EXOMEM_JWT_SIGNING_KEY="private-authority",
        EXOMEM_BASE_URL="https://archive.example",
    )
    stored = client.post(
        "/upload",
        files={"file": ("scan.pdf", b"%PDF-test", "application/pdf")},
        data={"scope": "Archive", "category": "Records"},
        headers={"authorization": "Bearer transport-secret"},
    ).json()
    authority = auth_sessions.SessionAuthority.local(
        directory=tmp_path / "sessions",
        signing_root="private-authority",
        issuer="https://archive.example",
        audience="https://archive.example/mcp",
    )
    bearer, session = asyncio.run(
        authority.issue(
            client_id="reader",
            scopes=["read"],
            identity=auth_sessions.SessionIdentity(github_user_id=42, github_login="sample-reader"),
        )
    )
    monkeypatch.setattr(server_auth, "build_session_authority", lambda **kwargs: authority)
    who = RequestPrincipal(
        "external",
        surface="mcp",
        issuer_family="test-issuer",
        client_binding=ClientBinding(authority.issuer, session.client_id),
        origin_session=OriginSessionBinding(
            session.session_id, session.generation, session.audience
        ),
    )
    with request_scope(who):
        handoff = commands.op_transfer_artifact(
            vault, operation="download", artifact=stored["path"]
        )
    copy = reserved_paths.copy_generic_file

    def invalidate(*args, **kwargs):
        copied = copy(*args, **kwargs)
        if invalidated == "expiry":
            monkeypatch.setattr(
                upload_tokens.time, "time", lambda: handoff["deliveries"]["https"]["expires_at"]
            )
        elif invalidated == "session":
            asyncio.run(authority.revoke_bearer(bearer, reason="test revocation"))
        else:
            governance = vault / "Knowledge Base/_Governance"
            (governance / "scopes").mkdir(parents=True)
            (governance / "rules").mkdir()
            (governance / "scopes/all.yaml").write_text(
                'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\npaths: ["Evidence/**"]\n'
            )
            (governance / "rules/all.yaml").write_text(
                "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
                "scope_ids: [01ARZ3NDEKTSV4RRFFQ69G5FAV]\naudience: external\nceiling: 1\n"
            )
        return copied

    # Run the route's real staging in a worker, where revoking the real local authority can run its own loop.
    from starlette.concurrency import run_in_threadpool

    from exomem import server_transfer

    monkeypatch.setattr(server_transfer, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(reserved_paths, "copy_generic_file", invalidate)
    denied = client.get(handoff["deliveries"]["https"]["url"])
    assert denied.status_code == 404
    assert denied.content == client.get("/download", params={"grant": "unavailable"}).content
    assert (vault / stored["path"]).read_bytes() == b"%PDF-test"


def test_application_logs_remove_artifact_authority(vault, caplog):
    import logging

    from exomem import artifact_recovery, upload_tokens
    from exomem.governance.principal import owner_principal

    token, _ = upload_tokens.mint_artifact(
        "private-authority",
        owner_principal(),
        {"path": "sample.bin"},
        deployment=artifact_recovery.deployment(vault),
        delivery=upload_tokens.ARTIFACT_HTTPS,
    )
    artifact_recovery.install_log_redaction()
    with caplog.at_level(logging.WARNING):
        logging.getLogger("uvicorn.access").warning("GET %s", "/download?grant=" + token)
        logging.getLogger("fastmcp.server.server").warning(
            "Resource %s", artifact_recovery.RESOURCE_PREFIX + token
        )
    assert token not in caplog.text
    assert "artifact-capability-removed" in caplog.text


def test_raw_release_and_receipt_use_the_computed_copy_digest(vault, monkeypatch, tmp_path):
    from test_governance_egress import _receipt_records

    from exomem import commands
    from exomem.governance import egress, raw_protection
    from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
    from exomem.writer_lease import invoke_command

    govern = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "govern_memory"
    )
    local = owner_principal(surface="library")
    who = RequestPrincipal("external", surface="mcp", issuer_family="test-issuer")
    with request_scope(local):
        saved = commands.op_preserve_evidence(
            vault,
            scope="Archive",
            category="Raw",
            filename="coordinates.csv",
            content="timestamp,latitude\n2026-10-01,12.345\n",
            raw_protection=True,
        )
        granted = invoke_command(
            govern,
            vault,
            operation="grant",
            scope="standing",
            grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB0",
            path=saved["path"],
            audience=who.audience_id,
            raw_release={
                "version": 1,
                "surface": who.surface,
                "issuer_family": who.issuer_family,
                "purpose": None,
                "includes_location": True,
            },
        )
    assert granted["ok"], granted
    original = (vault / saved["path"]).read_bytes()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    copied = reserved_paths.copy_generic_file(vault, saved["path"], private / "original")
    assert raw_protection.permits(vault, saved["path"], who, snapshot=original)
    read = reserved_paths.read_generic_bytes

    def no_original(root, path, **kwargs):
        assert path != saved["path"], "raw release reread the whole original"
        return read(root, path, **kwargs)

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", no_original)
    assert raw_protection.permits(vault, saved["path"], who, artifact_copy=copied)
    assert egress.release_allows_download(vault, saved["path"], principal=who, artifact_copy=copied)
    receipts = _receipt_records(vault)
    outcomes = [item for receipt in receipts for item in receipt.get("outcomes", [])]
    assert any(
        item.get("content_hash") == copied.sha256 and item.get("size") == copied.size
        for item in outcomes
    )
    with request_scope(local):
        invoke_command(
            govern,
            vault,
            operation="revoke",
            scope="standing",
            grant_id="01ARZ3NDEKTSV4RRFFQ69G5FB0",
        )
    assert not raw_protection.permits(vault, saved["path"], who, artifact_copy=copied)


@pytest.mark.parametrize("interruption", ["disconnect", "cancel"])
def test_private_spool_is_removed_when_the_stream_is_interrupted(
    vault, monkeypatch, tmp_path, interruption
):
    import asyncio
    from urllib.parse import parse_qs, urlsplit

    from starlette.requests import ClientDisconnect
    from test_upload_endpoint import _client

    from exomem import commands, server_transfer
    from exomem.governance.principal import owner_principal, request_scope

    client = _client(
        vault,
        monkeypatch,
        EXOMEM_UPLOAD_TOKEN="transport-secret",
        EXOMEM_JWT_SIGNING_KEY="private-authority",
        EXOMEM_BASE_URL="https://archive.example",
    )
    stored = client.post(
        "/upload",
        files={"file": ("scan.pdf", b"%PDF-test", "application/pdf")},
        data={"scope": "Archive", "category": "Records"},
        headers={"authorization": "Bearer transport-secret"},
    ).json()
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(
            vault, operation="download", artifact=stored["path"]
        )
    grant = parse_qs(urlsplit(handoff["deliveries"]["https"]["url"]).query)["grant"][0]
    copied_paths = []
    copy = reserved_paths.copy_generic_file

    def capture(*args, **kwargs):
        result = copy(*args, **kwargs)
        copied_paths.append(result.private_path)
        return result

    monkeypatch.setattr(reserved_paths, "copy_generic_file", capture)

    async def disconnected():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                if interruption == "cancel":
                    raise asyncio.CancelledError
                raise ClientDisconnect

        await server_transfer._OriginalDownloadResponse(vault, grant)(
            {"type": "http", "method": "GET", "headers": [], "extensions": {}}, receive, send
        )

    with pytest.raises(asyncio.CancelledError if interruption == "cancel" else ClientDisconnect):
        asyncio.run(disconnected())
    assert copied_paths and all(not path.parent.exists() for path in copied_paths)
    assert (vault / stored["path"]).read_bytes() == b"%PDF-test"


def test_native_admission_restores_signed_authorization_context_for_the_same_login(
    vault, monkeypatch
):
    from dataclasses import replace

    from exomem import artifact_recovery, upload_tokens
    from exomem.governance.authorization_session_lifecycle import AuthorizationSessionContext
    from exomem.governance.principal import ClientBinding, OriginSessionBinding, RequestPrincipal

    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "transport-secret")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "private-authority")
    context = AuthorizationSessionContext(
        "conversation", "external", "test-issuer", "cell", "vault", "keyring", 1, 2_000_000_000
    )
    issuer = RequestPrincipal(
        "external",
        surface="mcp",
        issuer_family="test-issuer",
        authorization_session_id=context.session_id,
        verified_authorization_session=context,
        client_binding=ClientBinding("https://archive.example", "reader"),
        origin_session=OriginSessionBinding("login", "generation", "https://archive.example/mcp"),
    )
    descriptor = {
        "path": "Knowledge Base/Evidence/Archive/scan.pdf",
        "sha256": "0" * 64,
        "size": 0,
        "file_name": "scan.pdf",
        "mime_type": "application/pdf",
        "mime_type_source": "stored",
    }
    token, _ = upload_tokens.mint_artifact(
        "private-authority",
        issuer,
        descriptor,
        deployment=artifact_recovery.deployment(vault),
        delivery=upload_tokens.ARTIFACT_NATIVE,
    )
    fresh_login = replace(
        issuer, authorization_session_id=None, verified_authorization_session=None
    )
    restored = artifact_recovery.native_principal(
        vault, artifact_recovery.RESOURCE_PREFIX + token, fresh_login
    )
    assert restored == issuer
    assert restored.verified_authorization_session == context
    with pytest.raises(artifact_recovery.RecoveryUnavailable):
        artifact_recovery.native_principal(
            vault,
            artifact_recovery.RESOURCE_PREFIX + token,
            replace(
                fresh_login,
                origin_session=replace(fresh_login.origin_session, session_id="another-login"),
            ),
        )


def test_original_selector_refuses_caller_host_locations_and_missing_signer(
    vault, monkeypatch, tmp_path
):
    from exomem import artifact_recovery, commands
    from exomem.governance.principal import owner_principal, request_scope

    external = tmp_path / "outside.bin"
    external.write_bytes(b"external bytes")
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "transport-secret")
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "private-authority")
    with request_scope(owner_principal(surface="mcp")):
        for selector in (str(external), "../outside.bin"):
            with pytest.raises(artifact_recovery.RecoveryUnavailable):
                commands.op_transfer_artifact(vault, operation="download", artifact=selector)
        monkeypatch.delenv("EXOMEM_JWT_SIGNING_KEY")
        with pytest.raises(ValueError, match="private signing authority is unavailable"):
            commands.op_transfer_artifact(vault, operation="download", artifact="missing.pdf")
    assert external.read_bytes() == b"external bytes"
