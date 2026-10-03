"""Cloud custody uses broker bytes without granting the cell public networking."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import socket
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp.server import dependencies

from exomem import client_artifacts, commands

FILE = {"file_id": "file-one", "download_url": "https://files.example/a?secret=yes", "file_name": "proof.bin"}
DATA = b"broker-owned original bytes"


def _cloud(monkeypatch: pytest.MonkeyPatch, operation="preserve_artifacts") -> None:
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "cell-one")
    monkeypatch.setenv("EXOMEM_CLOUD_ARTIFACT_BROKER_URL", "http://10.0.0.7:8767")
    descriptor = [FILE["file_id"], FILE["download_url"], None, FILE["file_name"]]
    digest = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    grant = jwt.encode(
        {"sub": "cell-one", "op": operation, "handles": [digest], "exp": int(time.time()) + 60},
        "test" * 8, algorithm="HS256",
    )
    monkeypatch.setattr(dependencies, "get_http_headers", lambda **_kw: {"x-exomem-artifact-grant": grant})
    monkeypatch.setattr(dependencies, "get_access_token", lambda: SimpleNamespace(claims={"sub": "cell-one", "iss": "exomem-cloud-cell"}))


class _BrokerConnection:
    def __init__(self, *_args, **_kwargs):
        self.sock = None
        self.body = io.BytesIO(DATA)

    def request(self, _method, _target, body, headers):
        assert json.loads(body)["operation"] in {"preserve_artifacts", "capture_source"}
        assert headers["Authorization"].startswith("Bearer ")

    def getresponse(self):
        return self

    status = 200

    def getheader(self, name):
        return {
            "Content-Length": str(len(DATA)), "Content-Type": "application/octet-stream",
            "X-Exomem-Artifact-SHA256": hashlib.sha256(DATA).hexdigest(),
            "X-Exomem-Artifact-Filename": base64.urlsafe_b64encode(b"proof.bin").decode(),
        }.get(name)

    def read(self, size):
        return self.body.read(size)

    def close(self):
        pass


@pytest.mark.parametrize("operation", ["preserve_artifacts", "capture_source"])
def test_cloud_preservation_stores_broker_bytes_without_cell_public_dns(vault: Path, source_schema, monkeypatch: pytest.MonkeyPatch, operation) -> None:
    """Catch the advertised Cloud handle path trying forbidden local DNS."""
    _cloud(monkeypatch, operation)
    attempted = []

    def denied_dns(*args, **_kwargs):
        attempted.append(args)
        raise OSError("cell DNS forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", denied_dns)
    from exomem import cloud_artifact_transport as transport
    monkeypatch.setattr(transport, "_PrivateConnection", _BrokerConnection)
    result = (commands.op_preserve_artifacts(vault, scope="case", category="raw", files=[FILE]) if operation == "preserve_artifacts" else commands.op_capture_source(vault, source_schema, title="Original file", source_type="research-report", files=[FILE]))
    assert attempted == [], "Cloud preservation attempted forbidden cell DNS"
    row = result["files"][0]
    assert row["outcome"] == "stored", result
    assert (vault / row["stored_path"]).read_bytes() == DATA
    assert row["hash"] == hashlib.sha256(DATA).hexdigest()


@pytest.mark.parametrize("failure", ["digest", "length", "filename", "mime", "broker", "grant", "endpoint"])
def test_cloud_refuses_unverified_bytes_without_final_artifact(vault, monkeypatch, failure):
    """Catch corrupt broker metadata and missing authority becoming stored data."""
    from exomem import cloud_artifact_transport as transport

    _cloud(monkeypatch)
    mkstemp = transport.tempfile.mkstemp
    monkeypatch.setattr(transport.tempfile, "mkstemp", lambda **kw: mkstemp(dir=vault.parent, **kw))
    class Broken(_BrokerConnection):
        status = 503 if failure == "broker" else 200
        def getheader(self, name):
            changed = {
                "digest": ("X-Exomem-Artifact-SHA256", "0" * 64),
                "length": ("Content-Length", "1"),
                "filename": ("X-Exomem-Artifact-Filename", "!bad!"),
                "mime": ("Content-Type", None),
            }.get(failure)
            return changed[1] if changed and name == changed[0] else super().getheader(name)

    monkeypatch.setattr(transport, "_PrivateConnection", Broken)
    if failure == "grant":
        monkeypatch.setattr(dependencies, "get_http_headers", lambda: {})
    if failure == "endpoint":
        monkeypatch.setenv("EXOMEM_CLOUD_ARTIFACT_BROKER_URL", "http://broker.example:8767")
    result = commands.op_preserve_artifacts(vault, scope="case", category="raw", files=[FILE])
    assert result["summary"]["failed"] == 1
    assert "stored_path" not in result["files"][0]
    assert not (vault / "Knowledge Base/Evidence/case/raw/proof.bin").exists()
    assert "secret" not in json.dumps(result)
    assert not list(vault.parent.glob("exomem-artifact-*"))


@pytest.fixture
def broker_authority():
    from exomem import artifact_broker as broker
    from exomem.cloud_artifact_transport import descriptor_digest

    key = Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    app = broker.ArtifactBroker(pem)
    now = int(time.time() * 1000)
    claims = {
        "v": 1, "aud": "exomem-artifact-broker", "sub": "cell-one", "op": "preserve_artifacts",
        "jti": str(uuid.uuid4()), "issued_ms": now, "exp": now // 1000 + 60,
        "handles": [descriptor_digest(FILE)] * 2, "max_files": 8, "max_bytes": 104857600,
    }
    body = {"file": FILE, "index": 0, "cell": "cell-one", "operation": "preserve_artifacts"}
    return app, key, claims, body


@pytest.mark.parametrize("changed", ["signature", "cell", "operation", "descriptor", "expired", "startup", "limits"])
def test_broker_refuses_altered_authority_before_fetch(broker_authority, changed):
    """Catch authenticated callers substituting a descriptor or broader authority."""
    app, key, claims, body = broker_authority
    if changed == "signature":
        key = Ed25519PrivateKey.generate()
    elif changed == "cell":
        body["cell"] = "another-cell"
    elif changed == "operation":
        body["operation"] = "capture_source"
    elif changed == "descriptor":
        body["file"] = {**FILE, "download_url": "https://elsewhere.example/secret"}
    elif changed == "expired":
        claims["exp"] = 1
    elif changed == "startup":
        claims["issued_ms"] = app.started_ms - 1
    else:
        claims["max_bytes"] += 1
    with pytest.raises(client_artifacts.SafeFetchError):
        app.claim(jwt.encode(claims, key, algorithm="EdDSA"), body)
    assert not app.active_cells


def test_broker_consumes_attempts_and_shares_budget_without_live_nonce_eviction(broker_authority):
    """Catch failed-attempt replay, per-file aggregate reset, or slot/cache overflow."""
    app, key, claims, body = broker_authority
    grant = jwt.encode(claims, key, algorithm="EdDSA")
    record = app.claim(grant, body)
    with pytest.raises(client_artifacts.SafeFetchError):
        app.claim(grant, {**body, "index": 1})
    app.release("cell-one")
    with pytest.raises(client_artifacts.SafeFetchError):
        app.claim(grant, body)
    record.budget.consume(client_artifacts.MAX_TOTAL_BYTES)
    assert app.claim(grant, {**body, "index": 1}) is record
    with pytest.raises(client_artifacts.SafeFetchError, match="size limit"):
        record.budget.validate_content_length(1)
    app.release("cell-one")
    app.active_cells.update({"a", "b", "c", "d"})
    fresh = {**claims, "jti": str(uuid.uuid4())}
    with pytest.raises(client_artifacts.SafeFetchError):
        app.claim(jwt.encode(fresh, key, algorithm="EdDSA"), body)
    app.active_cells.clear()
    app.max_records = 1
    with pytest.raises(client_artifacts.SafeFetchError):
        app.claim(jwt.encode(fresh, key, algorithm="EdDSA"), body)
    assert list(app.records) == [claims["jti"]]


def test_broker_direct_fetch_retains_ssrf_and_443_boundary(monkeypatch):
    """Catch the shared fetch seam allowing private addresses or alternate ports."""
    from exomem import cloud_artifact_transport as transport

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    for url in ("https://files.example/a", "https://files.example:8443/a"):
        with pytest.raises(client_artifacts.SafeFetchError):
            client_artifacts.stage_direct_artifact({**FILE, "download_url": url}, client_artifacts.FetchBudget(), allowed_ports=frozenset({443}))
    assert transport.descriptor_digest({**FILE, "file_name": "é.bin"}) == hashlib.sha256(json.dumps([FILE["file_id"], FILE["download_url"], None, "é.bin"], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_descriptor_digest_matches_schema_file_id_whitespace_normalization():
    """Catch valid schema-trimmed handles being refused by the broker grant."""
    from exomem.cloud_artifact_transport import descriptor_digest
    padded = {**FILE, "file_id": "\u0085\u001c file-one \u202f"}
    assert descriptor_digest(padded) == descriptor_digest(FILE)


@pytest.mark.parametrize("phase", ["fetch", "response", "worker", "timeout"])
def test_broker_cancellation_keeps_slots_until_worker_or_response_finishes(broker_authority, tmp_path, monkeypatch, phase):
    """Catch cancellation orphaning staged files or prematurely freeing slots."""
    app, key, claims, body = broker_authority
    from exomem import artifact_broker as broker
    if phase == "timeout":
        monkeypatch.setattr(broker, "_RESPONSE_SECONDS", 0.1)
    entered, release_worker = threading.Event(), threading.Event()
    staged_path = tmp_path / "staged.bin"
    def stage(_file, _budget, **_kwargs):
        entered.set()
        assert release_worker.wait(3)
        staged_path.write_bytes(DATA)
        return client_artifacts.StagedArtifact("file-one", staged_path, len(DATA), hashlib.sha256(DATA).hexdigest(), "application/octet-stream", "proof.bin")
    monkeypatch.setattr(client_artifacts, "stage_direct_artifact", stage)
    async def run():
        response_entered, cleaned, starts = asyncio.Event(), asyncio.Event(), []
        real_release = app.release
        def release(cell):
            real_release(cell)
            cleaned.set()
        monkeypatch.setattr(app, "release", release)
        queue = asyncio.Queue()
        queue.put_nowait({"type": "http.request", "body": json.dumps(body).encode()})
        async def send(event):
            if event["type"] == "http.response.start":
                starts.append(event)
                response_entered.set()
                await asyncio.Future()
        grant = jwt.encode(claims, key, algorithm="EdDSA")
        scope = {"type": "http", "path": "/internal/artifacts/fetch/v1", "method": "POST", "headers": [(b"authorization", f"Bearer {grant}".encode())]}
        task = asyncio.create_task(app(scope, queue.get, send))
        assert await asyncio.to_thread(entered.wait, 2)
        assert app.active_cells == {"cell-one"}
        if phase in {"response", "timeout"}:
            release_worker.set()
            await asyncio.wait_for(response_entered.wait(), 2)
            assert app.active_cells == {"cell-one"}
        if phase == "timeout":
            await asyncio.wait_for(task, 0.5)
            assert len(starts) == 1 and not app.active_cells and not staged_path.exists()
            return
        if phase == "worker":
            next(t for t in asyncio.all_tasks() if t.get_coro().__name__ == "to_thread").cancel()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        if phase != "response":
            assert app.active_cells == {"cell-one"}
            release_worker.set()
        await asyncio.wait_for(cleaned.wait(), 2)
        assert not app.active_cells and not staged_path.exists()
    try:
        asyncio.run(run())
    finally:
        release_worker.set()


def test_broker_http_stream_uses_canonical_safe_fetch_and_bounds_reads(broker_authority, tmp_path, monkeypatch):
    """Catch protocol drift, leaked temporary bytes, or unbounded request reads."""
    from exomem import artifact_broker as broker
    app, key, claims, body = broker_authority
    class Remote(_BrokerConnection):
        def putrequest(self, *_a, **_kw):
            pass
        def putheader(self, name, _value):
            assert name.lower() != "authorization"
        def endheaders(self):
            pass
    monkeypatch.setattr(client_artifacts, "_PinnedHTTPSConnection", lambda *_a, **_kw: Remote())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    mkstemp = client_artifacts.tempfile.mkstemp
    monkeypatch.setattr(client_artifacts.tempfile, "mkstemp", lambda **kw: mkstemp(dir=tmp_path, **kw))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
            headers = {"Authorization": "Bearer " + jwt.encode(claims, key, algorithm="EdDSA")}
            response = await client.post("/internal/artifacts/fetch/v1", json=body, headers=headers)
            assert response.status_code == 200 and response.content == DATA
            assert response.headers["x-exomem-artifact-sha256"] == hashlib.sha256(DATA).hexdigest()
            assert response.headers["content-length"] == str(len(DATA))
            assert not app.active_cells and not list(tmp_path.iterdir())
            duplicate = await client.post("/internal/artifacts/fetch/v1", json=body, headers=headers)
            assert duplicate.status_code == 403 and "secret" not in duplicate.text
            oversized = await client.post("/internal/artifacts/fetch/v1", content=b"x" * (broker._REQUEST_BYTES + 1))
            assert oversized.status_code == 403
            nested = await client.post("/internal/artifacts/fetch/v1", content=b"[" * 1500 + b"]" * 1500)
            assert nested.status_code == 403 and nested.json()["code"] == "SAFE_FETCH_FAILED"
            monkeypatch.setattr(broker, "_REQUEST_SECONDS", 0.01)
            with pytest.raises(TimeoutError):
                await app._body({"headers": []}, lambda: asyncio.Future())
            ready = await client.get("/health/ready")
            assert ready.status_code == 204 and not ready.content
    asyncio.run(run())
