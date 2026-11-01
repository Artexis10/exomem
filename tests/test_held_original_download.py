"""A download serves the original from a held handle: no private copy, and no hash that a record already proves.

These tests go through the cell's real `/download` route. Reads are counted at `os.read`
on the original's inode, so a test measures bytes rather than time or call order.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import types
from urllib.parse import quote

import pytest
from test_cloud_original_download import _get, _headers, _preserved_original
from test_cloud_upload_bridge import _run
from test_cloud_upload_bridge import cell as cell
from test_media_store import _offload
from test_media_store import selected_store as selected_store

from exomem import artifact_recovery, media_store, preserve, reserved_paths
from exomem.governance.principal import owner_principal

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


async def _stream(app, key, path, *, extra=None, on_body=None):
    """Drive `/download` at the ASGI layer and return every message the server would put on the wire."""
    headers = {**_headers(key, path, **(extra or {})), "host": "cell.local"}
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/download", "raw_path": b"/download", "root_path": "", "server": ("cell.local", 80),
             "query_string": f"path={quote(path, safe='')}".encode(), "client": ("127.0.0.1", 5000),
             "headers": [(name.encode(), value.encode()) for name, value in headers.items()]}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
        if on_body is not None and message["type"] == "http.response.body":
            await on_body()

    try:
        await app(scope, receive, send)
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

    async def change_after_first_chunk():
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
