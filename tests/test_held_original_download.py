"""A download serves the original from a held handle: no private copy, and no hash that a record already proves.

These tests go through the cell's real `/download` route. Reads are counted at `os.read`
on the original's inode, so a test measures bytes rather than time or call order.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import types
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from starlette.concurrency import run_in_threadpool
from test_cloud_original_download import _get, _headers, _preserved_original
from test_cloud_upload_bridge import _run
from test_cloud_upload_bridge import cell as cell
from test_media_store import _offload
from test_media_store import selected_store as selected_store

from exomem import (
    artifact_recovery,
    commands,
    media_store,
    preserve,
    reserved_paths,
    server_transfer,
)
from exomem.governance.principal import owner_principal, request_scope

RANGE = "bytes=100-199"


def _large_original(vault):
    """A preserved original of several response chunks, so a stream has a middle."""
    content = "".join(f"line {number}: an exact original, long enough to slice\n" for number in range(6000))
    captured = preserve.preserve(vault, scope="Test", category="samples", filename="large.txt", content=content)
    original = vault / captured.path
    return original, original.relative_to(vault).as_posix()


class _Reads:
    """Bytes read from one file's inode through `os.read`, however the caller reached the file."""

    def __init__(self, monkeypatch, original):
        self.inode = original.stat().st_ino
        self.total = 0
        real = os.read

        def counted(descriptor, size):
            data = real(descriptor, size)
            if os.fstat(descriptor).st_ino == self.inode:
                self.total += len(data)
            return data

        monkeypatch.setattr(os, "read", counted)
        # The held filesystem binds `os.read` at import, and its copy reads through that binding.
        from exomem import _held_fs_posix

        if hasattr(_held_fs_posix, "_read"):
            monkeypatch.setattr(_held_fs_posix, "_read", counted)


def _staying_client():
    """A `receive` for a client that stays: its empty request body, then silence until the response ends."""
    answered = False

    async def receive():
        nonlocal answered
        if not answered:
            answered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    return receive


async def _stream(app, key, path, *, extra=None, on_body=None, receive=None):
    """Drive `/download` at the ASGI layer and return every message the server would put on the wire."""
    headers = {**_headers(key, path, **(extra or {})), "host": "cell.local"}
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/download", "raw_path": b"/download", "root_path": "", "server": ("cell.local", 80),
             "query_string": f"path={quote(path, safe='')}".encode(), "client": ("127.0.0.1", 5000),
             "headers": [(name.encode(), value.encode()) for name, value in headers.items()]}
    sent = []

    async def send(message):
        sent.append(message)
        if on_body is not None and message["type"] == "http.response.body":
            await on_body(message)

    try:
        await app(scope, receive or _staying_client(), send)
    except (artifact_recovery.RecoveryUnavailable, reserved_paths.ReservedPathLeafError) as error:
        return sent, error
    return sent, None


def test_a_download_of_an_evicted_original_writes_no_second_copy(cell, selected_store, monkeypatch):
    key, app, vault = cell
    original, path = _preserved_original(vault)
    expected = original.read_bytes()
    _offload(vault, original)
    assert media_store.evict(vault, original)
    # The volume that held the private spool is full. The copy path refused any original above half its free space.
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda target: (
        types.SimpleNamespace(total=1, used=0, free=1) if "exomem-recovery-" in str(target) else real(target)))
    copies = []
    copy = reserved_paths.copy_generic_file
    monkeypatch.setattr(reserved_paths, "copy_generic_file",
                        lambda *args, **kwargs: copies.append(args[1]) or copy(*args, **kwargs))

    response = _run(app, lambda client: _get(client, path, _headers(key, path)))

    assert response.status_code == 200, response.text
    assert response.content == expected
    assert copies == []


def test_a_file_that_changes_during_the_response_never_gets_a_complete_looking_body(cell):
    key, app, vault = cell
    original, path = _large_original(vault)
    size = original.stat().st_size
    changed = []

    async def change_after_first_chunk(_message):
        if not changed:
            with original.open("r+b") as handle:  # in place, so the inode stays the same
                handle.write(b"X")
            changed.append(True)

    sent, error = _run(app, lambda client: _stream(app, key, path, on_body=change_after_first_chunk))

    assert changed and error is not None
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert all(message["more_body"] for message in bodies), "a stopped response sent its final chunk"
    assert sum(len(message["body"]) for message in bodies) < size, "a stopped response satisfied the length check"


@pytest.mark.parametrize("recorded_by", ["offload", "hydration"])
def test_a_range_on_a_file_that_matches_its_cache_identity_reads_no_byte_outside_the_range(
        cell, selected_store, monkeypatch, recorded_by):
    key, app, vault = cell
    original, path = _large_original(vault)
    data = original.read_bytes()
    _offload(vault, original)
    if recorded_by == "hydration":
        assert media_store.evict(vault, original)
        # The first request hydrates the original, which records the identity of the file it placed.
        assert _run(app, lambda client: _get(client, path, _headers(key, path, range=RANGE))).status_code == 206
    reads = _Reads(monkeypatch, original)

    response = _run(app, lambda client: _get(client, path, _headers(key, path, range=RANGE)))

    assert response.status_code == 206 and response.content == data[100:200]
    assert response.headers["x-exomem-sha256"] == hashlib.sha256(data).hexdigest()
    assert reads.total == 100


def test_a_file_that_no_longer_matches_its_cache_identity_is_hashed_once(cell, selected_store, monkeypatch):
    key, app, vault = cell
    original, path = _large_original(vault)
    data = original.read_bytes()
    _offload(vault, original)
    os.utime(original)  # the same bytes with a new change time, so the recorded identity no longer matches
    reads = _Reads(monkeypatch, original)

    response = _run(app, lambda client: _get(client, path, _headers(key, path, range=RANGE)))

    assert response.status_code == 206 and response.content == data[100:200]
    assert reads.total == len(data) + 100


def test_an_original_with_no_cache_identity_is_hashed_on_each_request(cell, monkeypatch):
    key, app, vault = cell
    original, path = _large_original(vault)
    size = original.stat().st_size
    reads = _Reads(monkeypatch, original)

    for request in (1, 2):
        _run(app, lambda client: _get(client, path, _headers(key, path, range=RANGE)))
        assert reads.total == request * (size + 100)


def test_a_file_edited_after_its_identity_was_recorded_is_not_served_on_the_strength_of_the_record(
        cell, selected_store):
    key, app, vault = cell
    original, path = _large_original(vault)
    _offload(vault, original)
    with original.open("r+b") as handle:
        handle.write(b"edited")

    response = _run(app, lambda client: _get(client, path, _headers(key, path)))

    assert response.status_code == 404


@pytest.mark.parametrize("cached", [False, True], ids=["resident", "recorded"])
def test_a_descriptor_that_does_not_match_the_expected_digest_refuses_before_the_first_byte(
        cell, selected_store, monkeypatch, cached):
    key, app, vault = cell
    original, path = _large_original(vault)
    if cached:
        _offload(vault, original)
    reads = _Reads(monkeypatch, original)

    with pytest.raises(artifact_recovery.RecoveryUnavailable):
        with artifact_recovery.stage(vault, path, owner_principal(surface="mcp"), expected_sha256="0" * 64):
            pytest.fail("a mismatched digest reached delivery")

    # A recorded file is refused on its record alone; a resident one needs its single hash first.
    assert reads.total == (0 if cached else original.stat().st_size)


def _grant_download(vault, monkeypatch):
    """Store a 200 KiB original through `/upload`, mint a download grant, and return the client, grant and file."""
    from test_upload_endpoint import _client

    client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="transport-secret",
                     EXOMEM_JWT_SIGNING_KEY="private-authority", EXOMEM_BASE_URL="https://archive.example")
    content = bytes(range(256)) * 800
    stored = client.post("/upload", files={"file": ("recording.wav", content, "audio/wav")},
                         data={"scope": "Archive", "category": "Recordings"},
                         headers={"authorization": "Bearer transport-secret"}).json()
    with request_scope(owner_principal(surface="mcp")):
        handoff = commands.op_transfer_artifact(vault, operation="download", artifact=stored["path"])
    url = handoff["deliveries"]["https"]["url"]
    return client, url, parse_qs(urlsplit(url).query)["grant"][0], vault / stored["path"], content


def _grant_stream(vault, grant, *, range_header=None, on_body=None, receive=None):
    """Drive the grant route at the ASGI layer; return every message it sends and the error that stopped it."""
    headers = [(b"range", range_header.encode())] if range_header else []
    scope = {"type": "http", "method": "GET", "headers": headers, "extensions": {}}
    sent = []

    async def send(message):
        sent.append(message)
        if on_body is not None and message["type"] == "http.response.body":
            await on_body(message)

    try:
        asyncio.run(server_transfer._OriginalDownloadResponse(vault, grant)(scope, receive or _staying_client(), send))
    except (artifact_recovery.RecoveryUnavailable, reserved_paths.ReservedPathLeafError) as error:
        return sent, error
    return sent, None


def test_a_multi_range_request_on_the_grant_route_returns_every_part_byte_exact(vault, monkeypatch):
    client, url, _, _, content = _grant_download(vault, monkeypatch)

    response = client.get(url, headers={"range": "bytes=0-9,100000-170000"})

    assert response.status_code == 206, response.text
    content_type = response.headers["content-type"]
    assert content_type.startswith("multipart/byteranges; boundary=")
    assert len(response.content) == int(response.headers["content-length"])
    delimiter = b"--" + content_type.split("boundary=")[1].encode()
    *parts, closing = response.content.split(delimiter)[1:]
    assert closing == b"--"
    served = {}
    for part in parts:
        head, _, body = part.partition(b"\r\n\r\n")
        served[head.decode().split("Content-Range: bytes ")[1].split("\r\n")[0]] = body.removesuffix(b"\r\n")
    assert served == {f"0-9/{len(content)}": content[0:10], f"100000-170000/{len(content)}": content[100000:170001]}


def test_a_file_rewritten_during_a_multipart_body_stops_it_without_the_final_boundary(vault, monkeypatch):
    _, _, grant, original, content = _grant_download(vault, monkeypatch)
    changed = []

    async def rewrite_after_the_first_part(message):
        if not changed and message["body"] == content[0:10]:
            with original.open("r+b") as handle:  # in place, so the inode stays the same
                handle.write(b"X")
            changed.append(True)

    sent, error = _grant_stream(vault, grant, range_header="bytes=0-9,100000-170000",
                                on_body=rewrite_after_the_first_part)

    assert changed and error is not None
    start = next(message for message in sent if message["type"] == "http.response.start")
    boundary = dict(start["headers"])[b"content-type"].split(b"boundary=")[1]
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert all(message["more_body"] for message in bodies), "a stopped response sent its final chunk"
    assert b"--" + boundary + b"--" not in b"".join(message["body"] for message in bodies)


def test_a_rename_after_the_last_chunk_does_not_fail_a_completed_download(vault, monkeypatch):
    _, _, grant, original, content = _grant_download(vault, monkeypatch)

    async def rename_after_the_last_chunk(message):
        if not message["more_body"]:
            original.rename(original.with_name("moved-" + original.name))

    sent, error = _grant_stream(vault, grant, on_body=rename_after_the_last_chunk)

    assert error is None
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert b"".join(message["body"] for message in bodies) == content


class _AbortedSeek:
    """A client that takes one full chunk of the body and then goes away, as a browser does on a seek.

    `receive` answers `http.disconnect` once that chunk is sent. With `hold`, the send that carries the chunk
    also waits for `hold`, as a send does under back-pressure, so the response is mid-body when the client leaves.
    """

    def __init__(self, reads=None, *, hold=None):
        self.reads, self.hold = reads, hold
        self.taken = asyncio.Event()
        self.read_when_it_left = None

    async def receive(self):
        await self.taken.wait()
        return {"type": "http.disconnect"}

    async def on_body(self, message):
        if self.taken.is_set() or len(message["body"]) != server_transfer._HeldFileResponse.chunk_size:
            return
        self.read_when_it_left = self.reads.total if self.reads else None
        self.taken.set()
        if self.hold:
            await self.hold.wait()


@pytest.mark.parametrize("range_header", [None, "bytes=100-"], ids=["simple", "ranged"])
def test_a_download_stops_reading_when_the_client_disconnects(cell, monkeypatch, range_header):
    key, app, vault = cell
    original, path = _large_original(vault)
    reads = _Reads(monkeypatch, original)
    seek = _AbortedSeek(reads)

    sent, error = _run(app, lambda client: _stream(
        app, key, path, extra={"range": range_header} if range_header else None,
        on_body=seek.on_body, receive=seek.receive))

    assert error is None
    assert reads.total - seek.read_when_it_left <= server_transfer._HeldFileResponse.chunk_size
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert all(message["more_body"] for message in bodies), "a body cut short by a disconnect looked complete"


def test_two_aborted_seeks_leave_both_download_slots_free(cell):
    key, app, vault = cell
    _, path = _large_original(vault)

    async def scenario(client):
        hold = asyncio.Event()
        seeks = [_AbortedSeek(hold=hold) for _ in range(2)]
        requests = [asyncio.create_task(_stream(
            app, key, path, extra={"range": "bytes=100-"}, on_body=seek.on_body, receive=seek.receive))
            for seek in seeks]
        try:
            # Each seek is stuck mid-body when it leaves. A response that ignores that holds its slot until
            # `hold` opens, so this wait ends only to report that it did not finish; it never decides a pass.
            await asyncio.wait(requests, timeout=20)
            return (await _get(client, path, _headers(key, path))).status_code
        finally:
            hold.set()
            await asyncio.gather(*requests)

    assert _run(app, scenario) == 200


def test_a_multi_range_download_stops_reading_when_the_client_disconnects(vault, monkeypatch):
    _, _, grant, original, content = _grant_download(vault, monkeypatch)
    # `_client` runs the threadpool inline, which never yields to the loop; a disconnect needs the real one.
    monkeypatch.setattr(server_transfer, "run_in_threadpool", run_in_threadpool)
    reads = _Reads(monkeypatch, original)
    seek = _AbortedSeek(reads)

    sent, error = _grant_stream(vault, grant, range_header="bytes=0-150000,160000-204799",
                                on_body=seek.on_body, receive=seek.receive)

    assert error is None
    assert reads.total - seek.read_when_it_left <= server_transfer._HeldFileResponse.chunk_size
    start = next(message for message in sent if message["type"] == "http.response.start")
    boundary = dict(start["headers"])[b"content-type"].split(b"boundary=")[1]
    sent_bytes = b"".join(message["body"] for message in sent if message["type"] == "http.response.body")
    assert b"--" + boundary + b"--" not in sent_bytes, "a body cut short by a disconnect looked complete"
    # The aborted request released its slot and pin: the next one gets the whole original.
    again, error = _grant_stream(vault, grant)
    assert error is None
    assert b"".join(message["body"] for message in again if message["type"] == "http.response.body") == content
