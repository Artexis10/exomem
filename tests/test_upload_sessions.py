"""Resumable upload sessions (tus 1.0) against the real routes, in process.

The interrupted upload that resumes through the real listener and worker is in
`test_local_ingress_e2e.py`. These cases cover what only a session can get
wrong: bytes that do not match their declared hash, a secret that must not
reveal a session, bytes left behind after a cancel or an expiry, and the bytes
a part keeps when its client drops mid-body. All data is invented.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

from exomem import server, upload_sessions

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
        with socket.create_connection((host, port), timeout=30) as raw:
            raw.sendall(
                f"PATCH {location} HTTP/1.1\r\nHost: {host}:{port}\r\nTus-Resumable: 1.0.0\r\n"
                f"Authorization: Bearer sekret\r\nExomem-Upload-Secret: {secret}\r\nUpload-Offset: 40\r\n"
                f"Content-Type: application/offset+octet-stream\r\nContent-Length: {len(data) - 40}\r\n"
                "Expect: 100-continue\r\n\r\n".encode()
            )
            # Uvicorn answers 100 once the handler first reads the body, so the bytes below
            # reach a reader that is already waiting for them.
            assert raw.recv(64).startswith(b"HTTP/1.1 100 ")
            raw.sendall(data[40:70])
            time.sleep(0.5)  # let the reader take them before the connection goes
        held = client.request("HEAD", location, headers={**TUS, "Exomem-Upload-Secret": secret})
        [part] = _parts(vault)

        assert held.status_code == 200 and held.headers["upload-offset"] == "70"
        assert part.read_bytes() == data[:70]
        assert _patch(client, location, secret, 70, data[70:]).status_code == 204
        state = _settled(client, location, secret)
        assert state["state"] == "committed", state
        assert state["receipt"]["hash"] == hashlib.sha256(data).hexdigest()
