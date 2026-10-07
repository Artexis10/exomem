"""Authenticated owner clients observe admitted data through the shipped MCP server."""

import asyncio
import io
import json
import shutil
from pathlib import Path

from starlette.testclient import TestClient
from test_connector_boundary import ISSUER, PRIVATE, PUBLIC
from test_connector_boundary import configured_boundary as configured_boundary

from exomem import preserve, server, server_auth, state_migration
from exomem.auth_sessions import SessionIdentity
from exomem.governance import principal


def test_oauth_owner_twins_keep_read_search_and_browse_independent_of_hidden_content(
    configured_boundary, vault, tmp_path, monkeypatch,
):
    """Real OAuth bearer verification precedes each producer on two independent vault roots."""
    assert Path(server.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    for name, value in {
        "GITHUB_CLIENT_ID": "fixture-client",
        "GITHUB_CLIENT_SECRET": "fixture-secret",
        "EXOMEM_GITHUB_USERNAME": "fixture-owner",
        "EXOMEM_JWT_SIGNING_KEY": "temporary-boundary-signing-root",
        "EXOMEM_UPLOAD_TOKEN": "temporary-boundary-transfer-secret",
    }.items():
        monkeypatch.setenv(name, value)
    authority = server_auth.build_session_authority(base_url=ISSUER)

    async def issue():
        identity = SessionIdentity(github_user_id=4242, github_login="fixture-owner")
        return [await authority.issue(client_id=client, scopes=["exomem:read", "exomem:write"], identity=identity)
                for client in ("limited", "full")]

    (limited, narrow_record), (full, broad_record) = asyncio.run(issue())
    assert narrow_record.github_user_id == broad_record.github_user_id == 4242
    artifacts = []
    with principal.request_scope(principal.owner_principal(surface="library")):
        for name, content in (("public.bin", b"\x00allowed bytes"), ("private.bin", b"\x00protected bytes")):
            captured = preserve.preserve(vault, scope="Workspace", category="Files", filename=name,
                content_stream=io.BytesIO(content), content_type="application/octet-stream")
            if name == "private.bin":
                sidecar = vault / captured.sidecar_path
                before = sidecar.read_text()
                after = before.replace("    projects: []", "    projects: [private-project]")
                assert after != before
                sidecar.write_text(after)
            artifacts.append(captured.path)
    twin = tmp_path / "twin"
    shutil.copytree(vault, twin)
    (twin / PRIVATE).write_text(
        "---\nproject: private-project\ntags: [protected-tag]\n---\n"
        "a different protected canary\n[[Notes/public]]\n" + "public information\n" * 100,
    )
    observations = []
    for index, root in enumerate((vault, twin)):
        monkeypatch.setenv("EXOMEM_VAULT_PATH", str(root))
        stop = state_migration.assert_offline_migration_authority(source="isolated authenticated twin setup")
        state_migration.arm_connector_boundary_offline(root, authority=stop)
        app = server.build_server(require_auth=True).http_app(stateless_http=True, json_response=True)
        with TestClient(app) as client:
            def call(token, name, arguments):
                response = client.post("/mcp", headers={
                    "Authorization": f"Bearer {token}", "Accept": "application/json",
                    "mcp-protocol-version": "2025-06-18", "mcp-method": "tools/call", "mcp-name": name,
                }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": name, "arguments": arguments,
                    "_meta": {"io.modelcontextprotocol/protocolVersion": "2025-06-18",
                              "io.modelcontextprotocol/clientCapabilities": {},
                              "io.modelcontextprotocol/clientInfo": {"name": "fixture", "version": "1"}},
                }})
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                return {key: value for key, value in result.items() if key != "_meta"}

            visible = call(limited, "read_memory", {"path": PUBLIC, "links": True, "include_history": True})
            assert "public information" in json.dumps(visible)
            hidden = call(limited, "read_memory", {"path": PRIVATE})
            broad = call(full, "read_memory", {"path": PRIVATE})
            assert "protected canary" in json.dumps(broad)
            assert "protected canary" not in json.dumps(hidden)
            search = call(limited, "ask_memory", {
                "query": "public information", "mode": "keyword", "detail": "full", "graph": True, "limit": 1,
            })
            assert "public.md" in json.dumps(search)
            listing = call(limited, "browse_memory", {"mode": "list", "path": "Knowledge Base/Notes"})
            assert "private.md" not in json.dumps(listing)
            # Exercise both cache orderings without multiplying the twin fixture.
            order = (limited, full) if index == 0 else (full, limited)
            transfers = {token: call(token, "transfer_artifact", {"operation": "download"})["structuredContent"]
                         for token in order}
            broad_transfer, transfer = transfers[full], transfers[limited]
            downloads = [client.get("/download", params={"path": path},
                headers={"Authorization": f"Bearer {transfer['token']}"}) for path in artifacts]
            assert downloads[0].status_code == 200
            assert downloads[0].content == b"\x00allowed bytes"
            assert downloads[1].status_code == 404
            private_download = client.get("/download", params={"path": artifacts[1]},
                headers={"Authorization": f"Bearer {broad_transfer['token']}"})
            assert private_download.status_code == 200
            assert private_download.content == b"\x00protected bytes"
            private_path = root / artifacts[1]
            private_bytes = private_path.read_bytes()
            private_path.unlink()
            try:
                missing = client.get("/download", params={"path": artifacts[1]},
                    headers={"Authorization": f"Bearer {transfer['token']}"})
            finally:
                private_path.write_bytes(private_bytes)
            assert (downloads[1].status_code, downloads[1].content) == (missing.status_code, missing.content)
            observations.append((visible, hidden, search, listing,
                                 [(response.status_code, response.content) for response in downloads]))
            if index == 1:
                asyncio.run(authority.tombstone(narrow_record.session_id, reason="operator"))
                revoked = client.get("/download", params={"path": artifacts[0]},
                    headers={"Authorization": f"Bearer {transfer['token']}"})
                assert revoked.status_code == 401
                assert b"allowed bytes" not in revoked.content
    assert observations[0] == observations[1]
