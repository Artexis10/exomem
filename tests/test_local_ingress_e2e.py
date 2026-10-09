"""Authenticated local ingress through a real managed worker process.

The shipped worker (`python -m exomem.service_manager worker`) runs as a
subprocess on a private Unix socket with the environment a manager with a
local listener gives it. The manager's `ServiceIngress` and `LocalListener`
sit in front of that socket exactly as `service_manager.serve` wires them.
All tokens and ids are synthetic.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import signal
import subprocess
import sys

import httpx
import pytest

from exomem import local_ingress
from exomem.auth_sessions import SessionAuthority, SessionIdentity
from exomem.governance import raw_protection
from exomem.service_ingress import (
    INGRESS_KEY_ENV,
    INGRESS_PROOF_HEADER,
    INGRESS_STAMP_HEADER,
    LocalListener,
    ServiceIngress,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Managed service runtime is Linux-only")

KEY = "e2e-manager-proof"
ROOT = "local-ingress-e2e-signing-root"
ISSUER = "https://memory.example"
OWNER_ID = 42
MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "local-e2e", "version": "1"},
    },
}


def test_threat_scenarios_hold_through_a_real_worker_behind_the_real_ingress(
    vault, tmp_path
) -> None:
    from exomem.service_manager import private_directory

    directory = private_directory(tmp_path / "private")
    socket_path = directory / "worker.sock"
    home = tmp_path / "auth-home"
    env = dict(os.environ)
    env.update(
        {
            "FASTMCP_HOME": str(home),
            "EXOMEM_BASE_URL": ISSUER,
            "GITHUB_CLIENT_ID": "fixture-client",
            "GITHUB_CLIENT_SECRET": "fixture-secret",
            "EXOMEM_GITHUB_USERNAME": "fixture-owner",
            "EXOMEM_GITHUB_USER_ID": str(OWNER_ID),
            "EXOMEM_JWT_SIGNING_KEY": ROOT,
            "EXOMEM_REST_API_KEY": "e2e-owner-rest-key",
            "EXOMEM_UPLOAD_TOKEN": "e2e-static-upload-token",
            # The public cap stands for the proxy edge's; loopback never crosses it.
            "EXOMEM_UPLOAD_MAX_BYTES": "64",
            "EXOMEM_LOCAL_UPLOAD_MAX_BYTES": "4096",
            "EXOMEM_UPLOAD_SESSION_MAX_BYTES": "65536",
            "EXOMEM_LOG_DIR": str(tmp_path / "logs"),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(tmp_path / "leases"),
            INGRESS_KEY_ENV: KEY,
        }
    )
    for name in ("EXOMEM_OAUTH_STORAGE_URL", "EXOMEM_WRITER_LEASE_URL"):
        env.pop(name, None)
    log = (tmp_path / "worker.log").open("w")

    def start_worker() -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-m",
                "exomem.service_manager",
                "worker",
                "--socket",
                str(socket_path),
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
            ],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    def stop(worker: subprocess.Popen) -> None:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait()

    workers = [start_worker()]
    identity = SessionIdentity(github_user_id=OWNER_ID, github_login="fixture-owner")
    session_secrets: list[str] = []

    async def scenario() -> None:
        local_authority = SessionAuthority.local(
            directory=home / "oauth-sessions",
            signing_root=ROOT,
            issuer=local_ingress.LOCAL_ISSUER,
            audience=local_ingress.LOCAL_AUDIENCE,
        )
        public_authority = SessionAuthority.local(
            directory=home / "oauth-sessions",
            signing_root=ROOT,
            issuer=ISSUER,
            audience=f"{ISSUER}/mcp",
        )
        local_token, local_record = await local_authority.issue(
            client_id="home", scopes=list(local_ingress.LOCAL_SCOPES), identity=identity
        )
        public_token, _ = await public_authority.issue(
            client_id="remote", scopes=["exomem:read", "exomem:write"], identity=identity
        )
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(socket_path), retries=0),
            base_url="http://localhost",
            trust_env=False,
            timeout=None,
        ) as upstream:

            async def healthy() -> None:
                async with asyncio.timeout(60):
                    while True:
                        if workers[-1].poll() is not None:
                            pytest.fail((tmp_path / "worker.log").read_text()[-5000:])
                        try:
                            if (await upstream.get("/health", timeout=0.5)).status_code == 200:
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)

            await healthy()
            ingress = ServiceIngress(ingress_key=KEY)
            ingress.resume(upstream)
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=LocalListener(ingress)),
                    base_url="http://127.0.0.1:8764",
                ) as local,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=ingress), base_url=ISSUER
                ) as public,
            ):

                def bearer(token: str) -> dict[str, str]:
                    return {**MCP_HEADERS, "authorization": f"Bearer {token}"}

                # R4: the audience is the ingress, both ways.
                accepted = await local.post("/mcp", headers=bearer(local_token), json=INITIALIZE)
                assert accepted.status_code == 200, accepted.text
                oauth_on_local = await local.post(
                    "/mcp", headers=bearer(public_token), json=INITIALIZE
                )
                assert oauth_on_local.status_code == 401
                assert "resource_metadata" not in oauth_on_local.headers.get(
                    "www-authenticate", ""
                )
                key_on_local = await local.post(
                    "/mcp", headers=bearer("e2e-owner-rest-key"), json=INITIALIZE
                )
                assert key_on_local.status_code == 401
                local_on_public = await public.post(
                    "/mcp", headers=bearer(local_token), json=INITIALIZE
                )
                assert local_on_public.status_code == 401
                assert "resource_metadata" in local_on_public.headers["www-authenticate"]
                oauth_on_public = await public.post(
                    "/mcp", headers=bearer(public_token), json=INITIALIZE
                )
                assert oauth_on_public.status_code == 200, oauth_on_public.text

                # R2: a forged stamp through the public listener is stripped,
                # even carrying the real proof.
                forged = await public.post(
                    "/mcp",
                    headers={
                        **bearer(local_token),
                        INGRESS_STAMP_HEADER.decode(): "local",
                        INGRESS_PROOF_HEADER.decode(): KEY,
                    },
                    json=INITIALIZE,
                )
                assert forged.status_code == 401
                assert "resource_metadata" in forged.headers["www-authenticate"]

                # R2: what an older, non-stripping manager would forward.
                unproven = await upstream.post(
                    "/mcp",
                    headers={**bearer(local_token), INGRESS_STAMP_HEADER.decode(): "local"},
                    json=INITIALIZE,
                )
                assert unproven.status_code == 403

                # R3: the door refuses before the worker, whatever the token.
                for headers, path, status in (
                    ({"cf-ray": "8f00000000000000-AMS"}, "/mcp", 403),
                    ({"origin": "https://evil.example"}, "/mcp", 403),
                    ({"host": "localhost:8764"}, "/mcp", 403),
                    ({}, "/control/promote", 404),
                    ({}, "/download", 404),
                    ({}, "/metrics.json", 404),
                ):
                    refused = await local.post(
                        path, headers={**bearer(local_token), **headers}, json=INITIALIZE
                    )
                    assert refused.status_code == status, (path, headers, refused.text)

                # R7: /upload with the local token, bytes only.
                upload = await local.post(
                    "/upload",
                    headers={"authorization": f"Bearer {local_token}"},
                    files={"file": ("note.txt", b"local e2e bytes", "text/plain")},
                    data={"scope": "Local", "category": "E2E"},
                )
                assert upload.status_code == 201, upload.text
                assert (vault / upload.json()["path"]).read_bytes() == b"local e2e bytes"

                # A private export over the public cap lands whole and owner-only
                # over loopback; the public listener still refuses its size.
                export = bytes(range(256)) * 4
                protected = await local.post(
                    "/upload",
                    headers={"authorization": f"Bearer {local_token}"},
                    files={"file": ("export.zip", export, "application/zip")},
                    data={"scope": "Local", "category": "Exports", "raw_protection": "1"},
                )
                assert protected.status_code == 201, protected.text
                stored = protected.json()["path"]
                assert raw_protection.marked(stored), stored
                assert (vault / stored).read_bytes() == export
                held = await local.post(
                    "/upload",
                    headers={"authorization": f"Bearer {local_token}"},
                    files={"file": ("export.zip", export, "application/zip")},
                    data={"hold": "1", "raw_protection": "1"},
                )
                assert held.status_code == 400, held.text
                public_upload = await public.post(
                    "/upload",
                    headers={"authorization": "Bearer e2e-static-upload-token"},
                    files={"file": ("export.zip", export, "application/zip")},
                    data={"scope": "Public", "category": "Exports"},
                )
                assert public_upload.status_code == 413, public_upload.text

                # R8: a file over both single-request caps goes in parts, and a
                # dropped part resumes at the offset the server reports.
                async def open_session(
                    client, credential: str, filename: str, data: bytes
                ) -> tuple[str, dict]:
                    metadata = {
                        "filename": filename,
                        "scope": "Local",
                        "category": "Sessions",
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    created = await client.post(
                        "/upload/sessions",
                        headers={
                            "authorization": f"Bearer {credential}",
                            "tus-resumable": "1.0.0",
                            "upload-length": str(len(data)),
                            "upload-metadata": ",".join(
                                f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in metadata.items()
                            ),
                        },
                    )
                    assert created.status_code == 201, created.text
                    session_secrets.append(created.headers["exomem-upload-secret"])
                    return created.headers["location"], {
                        "authorization": f"Bearer {credential}",
                        "tus-resumable": "1.0.0",
                        "exomem-upload-secret": created.headers["exomem-upload-secret"],
                    }

                async def send(client, location: str, headers: dict, offset: int, body) -> httpx.Response:
                    return await client.patch(
                        location,
                        headers={
                            **headers,
                            "upload-offset": str(offset),
                            "content-type": "application/offset+octet-stream",
                        },
                        content=body,
                    )

                async def committed(client, location: str, headers: dict) -> dict:
                    async with asyncio.timeout(30):
                        while True:
                            state = (await client.get(location, headers=headers)).json()
                            if state["state"] in ("committed", "failed"):
                                assert state["state"] == "committed", state
                                return state["receipt"]
                            await asyncio.sleep(0.05)

                export = bytes((n * 7) % 251 for n in range(12_000))
                location, headers = await open_session(local, local_token, "samples.bin", export)
                first = await send(local, location, headers, 0, export[:4000])
                assert first.status_code == 204, first.text
                assert first.headers["upload-offset"] == "4000"

                async def dropped():
                    yield export[4000:7000]
                    raise ConnectionResetError("the client's connection dropped")

                with contextlib.suppress(httpx.HTTPError, ConnectionError):
                    await send(local, location, headers, 4000, dropped())
                # The service restarts before the client returns: the new worker
                # re-reads the session and re-hashes the bytes it holds.
                ingress.pause()
                stop(workers[-1])
                workers.append(start_worker())
                await healthy()
                ingress.resume()
                held = await local.head(location, headers=headers)
                assert held.status_code == 200, held.text
                offset = int(held.headers["upload-offset"])
                assert 4000 <= offset <= 7000
                rest = await send(local, location, headers, offset, export[offset:])
                assert rest.status_code == 204, rest.text
                receipt = await committed(local, location, headers)
                assert receipt["hash"] == hashlib.sha256(export).hexdigest()
                assert (vault / receipt["path"]).read_bytes() == export

                # The public listener takes the same session with its upload token.
                small = b"public session bytes " * 10
                location, headers = await open_session(
                    public, "e2e-static-upload-token", "public-samples.bin", small
                )
                for start in range(0, len(small), 60):
                    part = await send(public, location, headers, start, small[start : start + 60])
                    assert part.status_code == 204, part.text
                receipt = await committed(public, location, headers)
                assert (vault / receipt["path"]).read_bytes() == small

                # R5: revocation closes the local door on the next request.
                await public_authority.tombstone(
                    local_record.session_id, reason="operator-revocation"
                )
                revoked = await local.post("/mcp", headers=bearer(local_token), json=INITIALIZE)
                assert revoked.status_code == 401
            await ingress.aclose()

    try:
        asyncio.run(scenario())
    finally:
        for worker in workers:
            stop(worker)
        log.close()
    logs = [(tmp_path / "worker.log").read_text()] + [
        path.read_text(errors="replace")
        for path in (tmp_path / "logs").rglob("*")
        if path.is_file()
    ]
    assert len(session_secrets) == 2
    for secret in (KEY, "e2e-owner-rest-key", "exo_s1.", *session_secrets):
        assert all(secret not in text for text in logs)
    assert any(re.search(r'ingress"?\s*[:=]\s*"?local', text) for text in logs)
