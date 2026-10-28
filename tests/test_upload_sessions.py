"""Resumable upload sessions (tus 1.0) against the real routes, in process.

The interrupted upload that resumes through the real listener and worker is in
`test_local_ingress_e2e.py`. These cases cover what only a session can get
wrong: bytes that do not match their declared hash, a secret that must not
reveal a session, bytes left behind after a cancel or an expiry, the bytes a
part keeps when its client drops mid-body, and which process may commit a
session that a stop interrupted. All data is invented.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from exomem import server, upload_sessions
from exomem.governance import principal as principal_module

TUS = {"Tus-Resumable": "1.0.0", "Authorization": "Bearer sekret"}


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-state"))


class _ASGIClient:
    """Synchronous facade; each request runs on its own loop, as in the upload tests."""

    def __init__(self, app) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())


def _app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("EXOMEM_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sekret")
    return server.build_server(require_auth=False).http_app()


def _client(vault: Path, monkeypatch: pytest.MonkeyPatch) -> _ASGIClient:
    from exomem import server_transfer

    async def inline_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(server_transfer, "run_in_threadpool", inline_threadpool)
    return _ASGIClient(_app(monkeypatch))


@contextlib.contextmanager
def _listening(app):
    """`app` behind a real uvicorn listener on a loopback port that the OS picks."""
    import uvicorn

    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    listener = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
    thread = threading.Thread(target=listener.run, kwargs={"sockets": [bound]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not listener.started:
        assert thread.is_alive() and time.monotonic() < deadline, "the listener never started"
        time.sleep(0.01)
    try:
        yield bound.getsockname()
    finally:
        listener.should_exit = True
        thread.join(30)
        bound.close()


def _cut_off_part(host: str, port: int, location: str, secret: str, offset: int, part: bytes, sent: int) -> None:
    """PATCH `part` at `offset` over a connection that drops after `sent` bytes of the body.

    Only a real listener reports a client that leaves mid-body.
    """
    with socket.create_connection((host, port), timeout=30) as raw:
        raw.sendall(
            f"PATCH {location} HTTP/1.1\r\nHost: {host}:{port}\r\nTus-Resumable: 1.0.0\r\n"
            f"Exomem-Upload-Secret: {secret}\r\nUpload-Offset: {offset}\r\n"
            f"Content-Type: application/offset+octet-stream\r\nContent-Length: {len(part)}\r\n"
            "Expect: 100-continue\r\n\r\n".encode()
        )
        # Uvicorn answers 100 once the handler first reads the body, so the bytes below
        # reach a reader that is already waiting for them.
        assert raw.recv(64).startswith(b"HTTP/1.1 100 ")
        raw.sendall(part[:sent])
        time.sleep(0.5)  # let the reader take them before the connection goes


def _create(client: _ASGIClient, data: bytes, *, sha256: str | None = None) -> tuple[str, str]:
    metadata = {
        "filename": "samples.json",
        "scope": "Device",
        "category": "Uploads",
        "sha256": sha256 or hashlib.sha256(data).hexdigest(),
    }
    created = client.request(
        "POST",
        "/upload/sessions",
        headers={
            **TUS,
            "Upload-Length": str(len(data)),
            "Upload-Metadata": ",".join(
                f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items()
            ),
        },
    )
    assert created.status_code == 201, created.text
    return created.headers["location"], created.headers["exomem-upload-secret"]


def _patch(client: _ASGIClient, location: str, secret: str, offset: int, chunk: bytes) -> httpx.Response:
    return client.request(
        "PATCH",
        location,
        headers={
            **TUS,
            "Exomem-Upload-Secret": secret,
            "Upload-Offset": str(offset),
            "Content-Type": "application/offset+octet-stream",
        },
        content=chunk,
    )


def _settled(client: _ASGIClient, location: str, secret: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.request("GET", location, headers={"Exomem-Upload-Secret": secret}).json()
        if state["state"] in ("committed", "failed"):
            return state
        time.sleep(0.05)
    pytest.fail("the session never settled")


def _open(vault: Path, data: bytes, *, binding: str = "bearer:test") -> tuple[upload_sessions.Session, str]:
    """A session for `data` opened through the store, as the creation route opens one."""
    return upload_sessions.create(
        vault,
        binding=binding,
        length=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        target={"filename": "samples.json", "scope": "Device", "category": "Uploads",
                "description": None, "raw_protection": False, "archive": None},
        max_bytes=upload_sessions.DEFAULT_MAX_BYTES,
    )


def _verified(vault: Path, data: bytes) -> tuple[upload_sessions.Session, str]:
    """A session holding all of `data`, whose commit nothing has started: what a stop leaves."""
    session, secret = _open(vault, data)
    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    assert patch.close() == (len(data), True)
    return session, secret


def _parts(vault: Path) -> list[Path]:
    from exomem.state_paths import vault_state_dir

    return list((vault_state_dir(vault) / upload_sessions.STORE_DIRNAME).glob("*.part"))


def test_bytes_that_do_not_match_their_hash_fail_and_are_deleted(vault, monkeypatch) -> None:
    client = _client(vault, monkeypatch)
    data = b'{"samples": [{"heart_rate": 61}]}'
    location, secret = _create(client, data, sha256=hashlib.sha256(b"other bytes").hexdigest())

    assert _patch(client, location, secret, 0, data).status_code == 204
    state = _settled(client, location, secret)

    assert state["state"] == "failed" and state["code"] == "UPLOAD_SHA256_MISMATCH"
    assert "receipt" not in state
    assert _parts(vault) == []


def test_a_source_capability_resumes_into_sources_and_reports_unassessed_compilation(vault, monkeypatch) -> None:
    """Raw material must retain its Source lane when its uploader needs resumable transport."""
    from exomem import upload_tokens

    client = _client(vault, monkeypatch)
    data = b"Raw material kept for a later note."
    handoff = upload_tokens.mint_for_endpoint("sekret", "https://example.invalid", lane="source")
    metadata = {"filename": "material.bin", "sha256": hashlib.sha256(data).hexdigest(),
                "source_kind": "session", "title": "Captured material", "scope": "Ignored", "category": "Ignored"}
    created = client.request("POST", "/upload/sessions", headers={
        **TUS, "Authorization": "Bearer " + handoff["token"], "Upload-Length": str(len(data)),
        "Upload-Metadata": ",".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items()),
    })
    assert created.status_code == 201, created.text
    location, secret = created.headers["location"], created.headers["exomem-upload-secret"]
    assert _patch(client, location, secret, 0, data[:10]).status_code == 204
    held = client.request("HEAD", location, headers={**TUS, "Exomem-Upload-Secret": secret})
    assert held.headers["upload-offset"] == "10"
    assert _patch(client, location, secret, 10, data[10:]).status_code == 204
    state = _settled(client, location, secret)
    receipt = state["receipt"]
    assert receipt["path"].startswith("Knowledge Base/Sources/")
    assert receipt["artifact_path"].startswith("Knowledge Base/Sources/")
    assert (vault / receipt["artifact_path"]).read_bytes() == data
    assert receipt["hash"] == hashlib.sha256(data).hexdigest()
    assert receipt["semantic_compilation"]["state"] == "unassessed"
    assert not (vault / "Knowledge Base/Evidence/Ignored").exists()


def test_a_source_session_replays_its_capture_after_the_upload_acknowledgement_is_lost(vault, monkeypatch) -> None:
    """A stopped upload commit must reuse its Source rather than allocate another uniquely named pair."""
    from exomem import server_transfer

    client = _client(vault, monkeypatch)
    data = b"One canonical Source despite an interrupted acknowledgement."
    target = {"lane": "source", "filename": "original.bin", "title": "One capture",
              "source_type": "session", "domain": None, "url": None, "content_type": None,
              "raw_protection": False}
    digest = hashlib.sha256(data).hexdigest()
    target["destination"] = server_transfer._source_session_destination(vault, target, size=len(data), sha256=digest)
    session, secret = upload_sessions.create(vault, binding="source-upload", length=len(data),
                                             sha256=digest, target=target, max_bytes=upload_sessions.DEFAULT_MAX_BYTES)
    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    patch.close()
    first = server_transfer.commit_session(vault, session.part, session.record)
    with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
        state = _settled(client, f"/upload/sessions/{session.id}", secret)
    assert state["receipt"] == first
    assert [path for path in (vault / target["destination"]).glob("*.bin")] == [vault / first["artifact_path"]]
    assert (vault / first["artifact_path"]).read_bytes() == data


def test_a_source_session_refuses_invalid_source_metadata_before_receiving_bytes(vault, monkeypatch) -> None:
    from exomem import upload_tokens

    client = _client(vault, monkeypatch)
    token = upload_tokens.mint("sekret", scope=upload_tokens.upload_scope("source"))
    metadata = {"filename": "article.bin", "sha256": "a" * 64, "source_kind": "article", "title": "An article without its URL"}
    created = client.request("POST", "/upload/sessions", headers={
        **TUS, "Authorization": "Bearer " + token, "Upload-Length": "100",
        "Upload-Metadata": ",".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items()),
    })
    assert created.status_code == 400, created.text
    assert _parts(vault) == []


@pytest.mark.parametrize("transport", ["resumable", "multipart"])
def test_source_upload_receipts_do_not_disclose_corpus_advisories(vault, monkeypatch, transport) -> None:
    """A private near-duplicate advisory must not escape through either byte transport."""
    import json
    import os
    import uuid

    from exomem import add, corpus_aware, upload_tokens
    from exomem.governance import egress

    client = _client(vault, monkeypatch)
    private = "Knowledge Base/Sources/Sessions/__exomem_raw_v1__restricted.md"
    existing = vault / private
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text(
        "---\ntype: source\nsource_type: session\ntitle: Restricted original\n"
        f"exomem_id: {uuid.uuid4()}\n---\n# Restricted original\nPrivate source body.\n"
    )
    who = principal_module.most_restrictive_principal(surface="transfer")
    with principal_module.request_scope(who):
        assert not egress.quick_page_visible(vault, private, principal=who)
        assert corpus_aware._index_admitter(vault)(private)

    class AdvisoryEnvironment:
        def get(self, key, default=None):
            return None if key == "EXOMEM_DISABLE_EMBEDDINGS" else os.environ.get(key, default)

    class AdvisoryOS:
        environ = AdvisoryEnvironment()

        def __getattr__(self, name):
            return getattr(os, name)

    # Only the advisory matcher is enabled; its invented score needs no model or provider.
    monkeypatch.setattr(add, "os", AdvisoryOS())
    monkeypatch.setattr(corpus_aware, "os", AdvisoryOS())
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", lambda *args, **kwargs: {private: 0.99})
    data = b"New original bytes."
    token = upload_tokens.mint("sekret", scope=upload_tokens.upload_scope("source"))
    values = {"filename": "material.bin", "sha256": hashlib.sha256(data).hexdigest(),
              "source_kind": "session", "title": "New original"}
    with principal_module.request_scope(who):
        if transport == "multipart":
            response = client.request("POST", "/upload", headers={"Authorization": "Bearer " + token},
                                      files={"file": (values["filename"], data)}, data=values)
            assert response.status_code == 201, response.text
            receipt = response.json()
        else:
            created = client.request("POST", "/upload/sessions", headers={
                **TUS, "Authorization": "Bearer " + token, "Upload-Length": str(len(data)),
                "Upload-Metadata": ",".join(f"{key} {base64.b64encode(value.encode()).decode()}"
                                            for key, value in values.items()),
            })
            assert created.status_code == 201, created.text
            location, secret = created.headers["location"], created.headers["exomem-upload-secret"]
            assert _patch(client, location, secret, 0, data).status_code == 204
            receipt = _settled(client, location, secret)["receipt"]
    assert private not in json.dumps(receipt)
    assert "Restricted original" not in json.dumps(receipt)
    assert "warnings" not in receipt and "structure_suggestion" not in receipt
    assert receipt["hash"] == values["sha256"]
    assert receipt["size"] == len(data)
    assert receipt["semantic_compilation"]["state"] == "unassessed"
    assert (vault / receipt["artifact_path"]).read_bytes() == data


def test_an_incomplete_source_rollback_retains_the_upload_without_another_capture(vault, monkeypatch) -> None:
    """A rollback-incomplete failure must not clear the mutation identity and rerun the Source writer."""
    from exomem import add, server_transfer
    from exomem import vault as vault_module

    _client(vault, monkeypatch)
    data = b"Bytes retained after an uncertain Source publication."
    digest = hashlib.sha256(data).hexdigest()
    target = {"lane": "source", "filename": "original.bin", "title": "Uncertain capture",
              "source_type": "session", "domain": None, "url": None, "content_type": None,
              "raw_protection": False}
    destination = server_transfer._source_session_destination(vault, target, size=len(data), sha256=digest)
    target["destination"] = destination
    session, _ = upload_sessions.create(vault, binding="source-upload", length=len(data),
                                        sha256=digest, target=target, max_bytes=upload_sessions.DEFAULT_MAX_BYTES)
    patch = upload_sessions.Patch(session, 0, len(data))
    patch.write(data)
    patch.close()
    folder = vault / destination

    def partial_publication(*args, **kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f"partial-{len(list(folder.glob('partial-*.md'))) + 1}.md"
        partial.write_text("# Partially published Source\n")
        raise vault_module.BatchWriteError(
            "BATCH_ROLLBACK_INCOMPLETE",
            vault_module.BatchTargetSummary(1, (partial.relative_to(vault).as_posix(),), 0), False,
        )

    monkeypatch.setattr(add, "add", partial_publication)
    for _ in range(2):
        with pytest.raises(upload_sessions.CommitFailed) as outcome:
            server_transfer.commit_session(vault, session.part, session.record)
        assert outcome.value.retryable
        assert session.part.read_bytes() == data
    assert [page.name for page in folder.glob("partial-*.md")] == ["partial-1.md"]


@pytest.mark.parametrize("filename", ["recording.wav", "original.md"])
def test_an_unsupported_write_format_is_visible_before_a_source_upload(vault, monkeypatch, filename) -> None:
    """Transport must not advertise or receive bytes for an original that the active writer cannot accept."""
    from types import SimpleNamespace

    from exomem import commands, vocabulary_authority

    client = _client(vault, monkeypatch)
    monkeypatch.setenv("EXOMEM_BASE_URL", "https://example.invalid")
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)

    class ActiveAuthority:
        def runtime_status(self):
            return SimpleNamespace(mode="v2")

    monkeypatch.setattr(vocabulary_authority, "VocabularyAuthority", lambda root: ActiveAuthority())
    handoff = commands.op_transfer_artifact(vault, operation="upload", lane="source")
    assert "resumable" not in handoff
    assert handoff["resumable_unavailable"]["code"] == "SOURCE_UPLOAD_UNAVAILABLE"
    metadata = {"filename": filename, "sha256": "a" * 64, "source_kind": "session"}
    created = client.request("POST", "/upload/sessions", headers={
        **TUS, "Authorization": "Bearer " + handoff["token"], "Upload-Length": "100",
        "Upload-Metadata": ",".join(f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items()),
    })
    assert created.status_code == 400, created.text
    assert _parts(vault) == []
    assert not (vault / "Knowledge Base" / "Evidence" / "Device" / "Uploads").exists()


def test_a_missing_or_wrong_secret_answers_as_an_unknown_session(vault, monkeypatch) -> None:
    client = _client(vault, monkeypatch)
    data = b"0123456789" * 10
    location, secret = _create(client, data)
    assert _patch(client, location, secret, 0, data[:40]).status_code == 204
    unknown = "/upload/sessions/" + "0" * 32
    other = "A" * 43

    def answers(path: str, presented: str | None) -> list[tuple[int, bytes]]:
        headers = {**TUS, "Upload-Offset": "40", "Content-Type": "application/offset+octet-stream"}
        if presented is not None:
            headers["Exomem-Upload-Secret"] = presented
        return [
            (response.status_code, response.content)
            for response in (
                client.request(method, path, headers=headers, content=b"x" if method == "PATCH" else None)
                for method in ("HEAD", "GET", "PATCH", "DELETE")
            )
        ]

    baseline = answers(unknown, secret)
    assert {status for status, _ in baseline} == {404}
    assert answers(location, None) == baseline
    assert answers(location, other) == baseline
    held = client.request("HEAD", location, headers={**TUS, "Exomem-Upload-Secret": secret})
    assert held.status_code == 200 and held.headers["upload-offset"] == "40"


def test_cancel_and_expiry_delete_the_bytes(vault, monkeypatch) -> None:
    client = _client(vault, monkeypatch)
    data = b"device_days" * 20
    cancelled, cancel_secret = _create(client, data)
    expiring, expiry_secret = _create(client, data)
    for location, secret in ((cancelled, cancel_secret), (expiring, expiry_secret)):
        assert _patch(client, location, secret, 0, data[:50]).status_code == 204
    assert len(_parts(vault)) == 2

    deleted = client.request("DELETE", cancelled, headers={**TUS, "Exomem-Upload-Secret": cancel_secret})
    assert deleted.status_code == 204
    gone = client.request("HEAD", cancelled, headers={**TUS, "Exomem-Upload-Secret": cancel_secret})
    assert gone.status_code == 404
    assert len(_parts(vault)) == 1

    later = upload_sessions._now() + upload_sessions.TTL_SECONDS + 1
    monkeypatch.setattr(upload_sessions, "_now", lambda: later)
    expired = client.request("HEAD", expiring, headers={**TUS, "Exomem-Upload-Secret": expiry_secret})
    assert expired.status_code == 410
    assert _parts(vault) == []


def test_a_malformed_size_setting_keeps_the_default_instead_of_failing_startup(
    vault, monkeypatch
) -> None:
    monkeypatch.setenv("EXOMEM_UPLOAD_SESSION_MAX_BYTES", "2GB")
    client = _client(vault, monkeypatch)

    options = client.request("OPTIONS", "/upload/sessions")

    assert options.status_code == 204
    assert options.headers["tus-max-size"] == str(upload_sessions.DEFAULT_MAX_BYTES)
    assert options.headers["tus-version"] == "1.0.0"


def test_one_credential_keeps_at_most_four_open_sessions(vault, monkeypatch) -> None:
    client = _client(vault, monkeypatch)
    opened = [_create(client, b"device_days")[0] for _ in range(upload_sessions.MAX_LIVE_PER_BINDING)]
    assert len(set(opened)) == 4

    refused = client.request(
        "POST",
        "/upload/sessions",
        headers={
            **TUS,
            "Upload-Length": "11",
            "Upload-Metadata": "filename c2FtcGxlcy5qc29u,scope RGV2aWNl,category VXBsb2Fkcw==,"
            f"sha256 {base64.b64encode(hashlib.sha256(b'device_days').hexdigest().encode()).decode()}",
        },
    )

    assert refused.status_code == 429 and refused.json()["code"] == "UPLOAD_SESSION_QUOTA"
    assert len(_parts(vault)) == 4


def test_a_part_whose_client_drops_mid_body_keeps_what_arrived_and_the_upload_resumes(
    vault, monkeypatch
) -> None:
    """A server that loses the bytes a cut-off part delivered makes the client send them again,
    and one that hashes them without writing them fails the whole file's SHA-256 at commit.
    Only a real listener reports a client that leaves mid-body."""
    data = b"device_days" * 10
    with _listening(_app(monkeypatch)) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=30
    ) as client:
        location, secret = _create(client, data)
        assert _patch(client, location, secret, 0, data[:40]).status_code == 204
        _cut_off_part(host, port, location, secret, 40, data[40:], 30)
        held = client.request("HEAD", location, headers={**TUS, "Exomem-Upload-Secret": secret})
        [part] = _parts(vault)

        assert held.status_code == 200 and held.headers["upload-offset"] == "70"
        assert part.read_bytes() == data[:70]
        assert _patch(client, location, secret, 70, data[70:]).status_code == 204
        state = _settled(client, location, secret)
        assert state["state"] == "committed", state
        assert state["receipt"]["hash"] == hashlib.sha256(data).hexdigest()


def test_an_agent_uploads_through_the_transfer_handoff_and_resumes_a_dropped_part(
    vault, monkeypatch
) -> None:
    """An agent with a shell gets a file past the proxy edge's one-request cap with nothing
    but what `transfer_artifact` returns: the creation URL, the minted token, the metadata
    keys and the header to resend. The handoff comes through the tool surface, as an agent
    receives it, and the token opens the session through the route's own credential check."""
    data = b'{"device_days": [{"samples": 1440, "heart_rate": 61}]}\n' * 8
    values = {"filename": "device_days.json", "sha256": hashlib.sha256(data).hexdigest(),
              "scope": "Device", "category": "Exports"}
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sekret")
    mcp = server.build_server(require_auth=False)
    with _listening(mcp.http_app()) as (host, port), httpx.Client(
        base_url=f"http://{host}:{port}", trust_env=False, timeout=30
    ) as client:
        monkeypatch.setenv("EXOMEM_BASE_URL", f"http://{host}:{port}")
        with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
            minted = asyncio.run(mcp.call_tool("transfer_artifact", {"operation": "upload"}, run_middleware=False))
        handoff = minted.structured_content
        resumable = handoff["resumable"]

        created = client.post(
            resumable["create_url"],
            headers={
                "Tus-Resumable": "1.0.0",
                "Authorization": f"Bearer {handoff['token']}",
                "Upload-Length": str(len(data)),
                "Upload-Metadata": ",".join(
                    f"{key} {base64.b64encode(values[key].encode()).decode()}" for key in resumable["metadata"]
                ),
            },
        )
        assert created.status_code == 201, created.text
        location, secret = created.headers["location"], created.headers[resumable["resend_header"]]
        tus = {"Tus-Resumable": "1.0.0", resumable["resend_header"]: secret}
        part = {**tus, "Content-Type": "application/offset+octet-stream"}
        first = client.patch(location, headers={**part, "Upload-Offset": "0"}, content=data[:100])
        _cut_off_part(host, port, location, secret, 100, data[100:], 150)
        offset = int(client.head(location, headers=tus).headers["upload-offset"])
        rest = client.patch(location, headers={**part, "Upload-Offset": str(offset)}, content=data[offset:])
        state = _settled(client, location, secret)

    assert (first.status_code, rest.status_code) == (204, 204)
    assert state["state"] == "committed", state
    assert state["receipt"]["hash"] == values["sha256"]
    assert (vault / state["receipt"]["path"]).read_bytes() == data


#: One process of a given kind built against the test's vault and state, run to quiescence:
#: its activation finished and any commit it started joined.
_PROCESS = """
import sys
import threading

from exomem import server, service_standby

kind = sys.argv[1]
if kind == "standby":
    service_standby.enter_standby()
mcp = server.build_server(require_auth=False)
if kind != "stdio":
    mcp.http_app()  # what an HTTP listener serves; a stdio server never builds it
activation = mcp._exomem_local_runtime_activation
activation.start()  # what liveness or the fallback timer does; a standby defers it
if activation._thread is not None:
    activation._thread.join(120)
for thread in threading.enumerate():
    if thread.name.startswith("upload-session-commit"):
        thread.join(120)
"""


@pytest.mark.parametrize(
    ("kind", "settles"), [("serving", "committed"), ("standby", "verifying"), ("stdio", "verifying")]
)
def test_only_the_serving_runtime_finishes_a_commit_that_a_stop_interrupted(
    vault, tmp_path, monkeypatch, kind, settles
) -> None:
    """A standby owns nothing until promotion and a stdio server serves no session route.
    One that commits writes Evidence beside the worker that owns it, which then finds the
    bytes gone and fails an upload whose file is already in the vault."""
    monkeypatch.setenv("EXOMEM_UPLOAD_TOKEN", "sekret")
    data = b'{"device_days": [{"samples": 1440}]}'
    session, secret = _verified(vault, data)

    ran = subprocess.run(
        [sys.executable, "-c", _PROCESS, kind], cwd=tmp_path, capture_output=True, text=True, timeout=300
    )

    assert ran.returncode == 0, ran.stderr[-4000:]
    assert upload_sessions.open_session(vault, session.id, secret).record["state"] == settles
    stored = vault / "Knowledge Base" / "Evidence" / "Device" / "Uploads" / "samples.json"
    assert stored.exists() == (settles == "committed")


_SECOND_COMMITTER = """
import sys
import threading
from pathlib import Path

from exomem import upload_sessions

ran = []
for pending in upload_sessions.startup_sweep(Path(sys.argv[1])):
    upload_sessions.start_commit(pending, lambda part, record: ran.append(1) or {"hash": "second"})
for thread in threading.enumerate():
    if thread.name.startswith("upload-session-commit"):
        thread.join(60)
print(len(ran))
"""


def test_a_second_process_never_commits_a_session_already_being_committed(vault, tmp_path) -> None:
    """Two processes committing one session both preserve it; the slower one then fails on the
    deleted `.part` and leaves `failed` over evidence that exists."""
    session, secret = _verified(vault, b"device_days" * 20)
    entered, release = threading.Event(), threading.Event()

    def first(part: Path, record: dict) -> dict:
        entered.set()
        release.wait(60)
        return {"hash": "first"}

    upload_sessions.start_commit(session, first)
    assert entered.wait(30)
    try:
        second = subprocess.run(
            [sys.executable, "-c", _SECOND_COMMITTER, str(vault)],
            cwd=tmp_path, capture_output=True, text=True, timeout=300,
        )
    finally:
        release.set()
    deadline = time.monotonic() + 30
    while (record := upload_sessions.open_session(vault, session.id, secret).record)["state"] != "committed":
        assert time.monotonic() < deadline, record
        time.sleep(0.05)

    assert second.returncode == 0, second.stderr[-4000:]
    assert second.stdout.split() == ["0"]
    assert record["receipt"] == {"hash": "first"}


def test_a_commit_resumed_after_its_file_landed_answers_already_stored(vault, monkeypatch) -> None:
    """A commit stopped after the preserve but before its `committed` record fails ARTIFACT_EXISTS
    on every retry, so the client never gets a receipt for a file that is stored."""
    from exomem import preserve

    client = _client(vault, monkeypatch)
    data = b'{"device_days": [{"samples": 1440}]}'
    session, secret = _verified(vault, data)
    # What the stopped commit had already done: the file and its page are in the vault.
    landed = preserve.preserve_stream(
        vault, scope="Device", category="Uploads", filename="samples.json", stream=io.BytesIO(data)
    )

    state = _settled(client, f"/upload/sessions/{session.id}", secret)

    assert state["state"] == "committed", state
    assert (state["receipt"]["state"], state["receipt"]["path"]) == ("already_stored", landed.path)
    folder = vault / "Knowledge Base" / "Evidence" / "Device" / "Uploads"
    assert sorted(path.name for path in folder.iterdir()) == ["samples.json", "samples.json.md"]


@pytest.mark.parametrize(
    ("filename", "status", "code"), [("???", 400, "INVALID_PRESERVE"), ("samples.json", 409, "ARTIFACT_EXISTS")]
)
def test_a_session_whose_file_cannot_land_is_refused_before_any_byte_is_sent(
    vault, monkeypatch, filename, status, code
) -> None:
    """Checked only at commit, a bad name or a taken one fails after the whole upload."""
    from exomem import preserve

    client = _client(vault, monkeypatch)
    preserve.preserve_stream(
        vault, scope="Device", category="Uploads", filename="samples.json", stream=io.BytesIO(b"{}")
    )
    metadata = {"filename": filename, "scope": "Device", "category": "Uploads",
                "sha256": hashlib.sha256(b"device_days").hexdigest()}

    refused = client.request(
        "POST",
        "/upload/sessions",
        headers={
            **TUS,
            "Upload-Length": "11",
            "Upload-Metadata": ",".join(
                f"{key} {base64.b64encode(value.encode()).decode()}" for key, value in metadata.items()
            ),
        },
    )

    assert (refused.status_code, refused.json()["code"]) == (status, code)
    assert _parts(vault) == []


def test_concurrent_creates_never_open_more_than_the_per_credential_limit(vault, monkeypatch) -> None:
    """Creation counts open sessions and then writes its own; creates between those steps all pass."""
    real = shutil.disk_usage

    def loaded_disk(path):
        time.sleep(0.05)  # a loaded disk answers slowly, between the count and the write
        return real(path)

    monkeypatch.setattr(shutil, "disk_usage", loaded_disk)
    barrier = threading.Barrier(12)
    outcomes: list[str] = []

    def create() -> None:
        barrier.wait()
        try:
            _open(vault, b"device_days", binding="bearer:one")
            outcomes.append("opened")
        except upload_sessions.SessionError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=create) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert outcomes.count("opened") == upload_sessions.MAX_LIVE_PER_BINDING
    assert len(_parts(vault)) == upload_sessions.MAX_LIVE_PER_BINDING


def test_a_cancel_read_before_the_final_part_never_deletes_the_verified_upload(vault) -> None:
    """A DELETE that acts on the state it opened with removes bytes already being committed."""
    data = b"device_days" * 10
    session, secret = _open(vault, data)
    cancel = upload_sessions.open_session(vault, session.id, secret)
    patch = upload_sessions.Patch(upload_sessions.open_session(vault, session.id, secret), 0, len(data))
    patch.write(data)
    patch.close()

    with pytest.raises(upload_sessions.SessionError) as refused:
        upload_sessions.delete(cancel)

    assert refused.value.status == 409
    assert [part.read_bytes() for part in _parts(vault)] == [data]


def test_a_number_too_long_to_parse_is_a_bad_request(vault, monkeypatch) -> None:
    """`int()` refuses a 5000-digit string with ValueError, which the routes answered as a 500."""
    client = _client(vault, monkeypatch)
    location, secret = _create(client, b"device_days")
    huge = "9" * 5000

    created = client.request(
        "POST",
        "/upload/sessions",
        headers={**TUS, "Upload-Length": huge, "Upload-Metadata": "filename c2FtcGxlcy5qc29u"},
    )
    patched = client.request(
        "PATCH",
        location,
        headers={**TUS, "Exomem-Upload-Secret": secret, "Upload-Offset": huge,
                 "Content-Type": "application/offset+octet-stream"},
        content=b"device_days",
    )

    assert (created.status_code, patched.status_code) == (400, 400)
