"""The holding local client's CLI cancels through verified REST, without a fresh owner identity."""

from __future__ import annotations

import asyncio
import json

import httpx
from starlette.middleware import Middleware
from test_local_ingress_worker import (
    _authorities,
    _doors,
    _Harness,
    _issue_local,
)
from test_local_ingress_worker import (
    managed as managed,
)

from exomem import (
    artifact_lifecycle,
    capabilities,
    commands,
    held_uploads,
    local_ingress,
    schema,
    server,
    server_rest,
    server_transfer,
)
from exomem.__main__ import _core_op_main
from exomem.governance.authorization_transport import AuthorizationSessionMiddleware
from exomem.public_surface import build_tools, public_surface


def test_cli_uses_the_holding_local_session_and_releases_its_upload_capacity(vault, tmp_path, managed, capsys):
    """A different valid client cannot cancel the upload; the holding client can replace it afterward."""
    managed.setenv("EXOMEM_LOCAL_UPLOAD_MAX_BYTES", "16")
    public, local = _authorities(tmp_path)
    mcp = server.ExomemFastMCP("held-upload-cancellation", auth=None)
    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "transfer_artifact")
    mcp.add_middleware(server.PublicCommandNameMiddleware((command,), vault))
    mcp.add_middleware(AuthorizationSessionMiddleware(vault))
    mcp.add_middleware(server.RenderedRefusalMiddleware())
    descriptor = capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True,
        product_commands=public_surface().listed_names((command,)),
        contributing_commands=(command,),
    )
    mcp.tool(commands.bind_vault(command.leaf, vault, name=command.name,
                               command=command, surface_descriptor=descriptor), name=command.name)
    discovery = server.PreferredToolDiscovery(frozenset())
    discovery.public_tools = build_tools((command,))
    mcp.add_transform(discovery)
    config = server_transfer.register_transfer_routes(mcp, vault_root=vault, media_worker=None)
    server_rest.register_rest_facade(mcp, vault_root=vault, source_schema=schema.load_source_schema(vault),
                                    transfer_config=config)
    gate = Middleware(local_ingress.LocalIngressMiddleware, verifier=local_ingress.LocalCredentialVerifier(local))
    harness = _Harness(mcp.http_app(stateless_http=True, json_response=True, middleware=[gate]), public, local)
    managed.setattr(artifact_lifecycle, "_supported", frozenset(artifact_lifecycle.LIFECYCLE_IDS))
    managed.setattr(artifact_lifecycle, "_activated", set())

    async def scenario():
        owner_token, _ = await _issue_local(harness)
        other_token, _ = await _issue_local(harness, client_id="another-client")
        owner_file, other_file = tmp_path / "owner.token", tmp_path / "other.token"
        owner_file.write_text(owner_token)
        other_file.write_text(other_token)
        async with _doors(harness) as doors:
            original = b"original bytes"
            headers = {"Authorization": "Bearer " + owner_token}

            async def upload():
                return await doors.local.post("/upload", headers=headers,
                    files={"file": ("original.txt", original, "text/plain")},
                    data={"hold": "1", "lane": "source"})

            held = await upload()
            assert held.status_code == 201, held.text
            reference = held.json()["file"]["download_url"]
            capped = await upload()
            assert capped.status_code != 201
            assert capped.json()["code"] == "HELD_UPLOAD_QUOTA"
            loop = asyncio.get_running_loop()

            class LocalTransport(httpx.BaseTransport):
                def handle_request(self, request):
                    body = request.read()
                    async def invoke():
                        return await doors.local.request(request.method, request.url.path,
                                                         headers=request.headers, content=body)
                    response = asyncio.run_coroutine_threadsafe(invoke(), loop).result(timeout=15)
                    return httpx.Response(response.status_code, headers=response.headers, content=response.content)

            def invoke(token_file, references):
                return _core_op_main([
                    "transfer_artifact", "--operation", "cancel",
                    *(token for reference in references for token in ("--uploads", reference)),
                    "--token-file", str(token_file), "--port", "8764", "--json",
                ], local_transfer_transport=LocalTransport())

            assert await asyncio.to_thread(invoke, other_file, [reference, "exomem-held:" + "x"*43]) == 0
            refused = json.loads(capsys.readouterr().out)["data"]
            assert refused["uploads"] == [
                {"index": 0, "state": "unavailable"}, {"index": 1, "state": "unavailable"},
            ]
            assert reference not in json.dumps(refused)
            from exomem import reserved_paths, writer_lease

            def no_canonical_work(*args, **kwargs):
                raise AssertionError("cancellation entered canonical writer or global cleanup")

            # The real dispatcher must not acquire canonical custody or scan unrelated uploads.
            with managed.context() as cancelling:
                cancelling.setattr(writer_lease.LeaseManager, "ensure_writer", no_canonical_work)
                cancelling.setattr(reserved_paths, "warm_identity_catalogue_before_boundary", no_canonical_work)
                cancelling.setattr(held_uploads, "_sweep", no_canonical_work)
                assert await asyncio.to_thread(invoke, owner_file, [reference, reference]) == 0
            cancelled = json.loads(capsys.readouterr().out)["data"]
            assert cancelled["uploads"] == [
                {"index": 0, "state": "cancelled", "released_bytes": len(original)},
                {"index": 1, "state": "unavailable"},
            ]
            assert reference not in json.dumps(cancelled)
            replacement = await upload()
            assert replacement.status_code == 201, replacement.text
            from test_local_ingress_worker import PROTOCOL_VERSION, _mcp_body

            call = _mcp_body(100)
            call["params"]["name"] = "cancel_uploads"
            call["params"]["arguments"] = {"uploads": [replacement.json()["file"]["download_url"]]}
            mcp_reply = await doors.local.post("/mcp", headers={
                **headers, "accept": "application/json", "content-type": "application/json",
                "mcp-protocol-version": PROTOCOL_VERSION, "mcp-method": "tools/call", "mcp-name": "cancel_uploads",
            }, json=call)
            assert mcp_reply.status_code == 200, mcp_reply.text
            result = mcp_reply.json()["result"]
            assert result.get("isError") is not True, result
            data = result["structuredContent"]
            data = data.get("result", data)
            assert data["cancelled"] == 1, data

    asyncio.run(scenario())


def test_cli_forwards_the_protected_authorization_session_only_to_the_local_listener(tmp_path, capsys):
    """A hold bound to an authorization session is cancellable only when the CLI carries that same session."""
    import os
    import secrets

    from exomem.governance import authorization_sessions

    credential = authorization_sessions._encode_credential(secrets.token_bytes(16), secrets.token_bytes(32))
    token_file = tmp_path / "owner.token"
    token_file.write_text("holding-local-token")
    reference = held_uploads.HELD_SCHEME + "y" * 43
    seen = []

    class Capture(httpx.BaseTransport):
        def handle_request(self, request):
            seen.append((request.url.host, request.url.path, dict(request.headers), request.read().decode()))
            return httpx.Response(200, json={"success": True, "data": {
                "cancelled": 1, "unavailable": 0, "failed": 0,
                "uploads": [{"index": 0, "state": "cancelled", "released_bytes": 3}]}})

    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, credential.encode("ascii"))
        os.close(write_fd)
        write_fd = -1
        code = _core_op_main([
            "transfer_artifact", "--operation", "cancel", "--uploads", reference,
            "--authorization-session-fd", str(read_fd),
            "--token-file", str(token_file), "--port", "8764", "--json",
        ], local_transfer_transport=Capture())
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
    output = capsys.readouterr()
    assert code == 0
    [(host, path, headers, body)] = seen
    assert (host, path) == ("127.0.0.1", "/api/transfer_artifact")
    assert headers["x-exomem-authorization-session"] == credential
    assert headers["authorization"] == "Bearer holding-local-token"
    assert credential not in body
    assert credential not in output.out + output.err
    assert json.loads(output.out)["data"]["cancelled"] == 1
