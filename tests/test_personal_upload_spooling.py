import asyncio
import io
import json
import shutil
import tempfile
from threading import Event
from types import SimpleNamespace

import pytest
from starlette import datastructures, formparsers
from starlette.requests import ClientDisconnect, Request
from test_agent_upload_handoff import _post, _prepare
from test_agent_upload_handoff import upload_env as upload_env
from test_upload_endpoint import _client

from exomem import held_uploads, multipart_upload, originals, upload_handoff, upload_stage


@pytest.fixture
def observed_spools(tmp_path, monkeypatch):
    async def inline(function, *args):
        return function(*args)

    # The ASGI fixtures use inline workers to avoid the Python 3.14 AnyIO return-path hang.
    monkeypatch.setattr(datastructures, "run_in_threadpool", inline)
    monkeypatch.setattr(multipart_upload, "run_in_threadpool", inline)
    # Keep the actual Starlette spool on the fixture vault's volume.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    spools = []
    disk_writes = []

    class ObservedSpool(tempfile.SpooledTemporaryFile):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            spools.append(self)

        def write(self, data):
            written = super().write(data)
            if self._rolled:
                disk_writes.append(self.tell())
            return written

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", ObservedSpool)
    return spools, disk_writes


@pytest.mark.parametrize("route", ["ordinary", "ticket"])
@pytest.mark.parametrize("can_roll", [False, True], ids=["refuse-rollover", "refuse-append"])
def test_upload_refuses_before_a_spool_write_crosses_the_floor(vault, monkeypatch, request, route, can_roll, observed_spools):
    monkeypatch.delenv(originals.CONFIG_ENV, raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    config = originals.local_configuration_path(vault)
    config.parent.mkdir(parents=True, exist_ok=True)
    floor = 1024**2
    config.write_text(json.dumps({"vaults": {str(vault): {"store": "test", "upload_floor_bytes": floor}}}))
    spools, disk_writes = observed_spools
    rollover = formparsers.MultiPartParser.spool_max_size
    chunk = 64 * 1024  # HTTPX's read size for a streamed file object.
    headroom = rollover + (chunk if can_roll else 0)
    data = b"x" * (rollover + 2 * chunk)

    def disk_usage(_path):
        free = floor + headroom - (disk_writes[-1] if disk_writes else 0)
        return SimpleNamespace(total=10 * floor, used=10 * floor - free, free=free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    if route == "ticket":
        env = request.getfixturevalue("upload_env")
        response = _post(env, _prepare(env, data), io.BytesIO(data), filename="recording.bin")
    else:
        client = _client(vault, monkeypatch, EXOMEM_UPLOAD_TOKEN="sekret")
        response = client.post(
            "/upload", headers={"Authorization": "Bearer sekret"}, data={"scope": "test", "category": "samples"},
            files={"file": ("recording.bin", io.BytesIO(data), "application/octet-stream")},
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "MEDIA_SPACE_BUSY"
    assert "Retry after eviction frees space" in response.json()["reason"]
    if route == "ticket":
        assert upload_handoff.SPENT_REMEDY in response.json()["reason"]
    assert disk_writes == ([rollover + chunk] if can_roll else [])
    assert len(spools) == 1 and spools[0].closed
    assert not list(vault.rglob("recording.bin"))
    assert not list(upload_stage.stage_root(vault).glob("*.part"))
    assert not list(held_uploads._store(vault).glob("*.bin"))


@pytest.mark.parametrize("interruption", [ClientDisconnect, asyncio.CancelledError])
def test_interrupted_multipart_closes_its_rolled_spool(vault, observed_spools, interruption):
    spools, disk_writes = observed_spools
    data = b"x" * (formparsers.MultiPartParser.spool_max_size + 64 * 1024)

    async def send():
        waiting = asyncio.Event()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                prefix = b'--test-boundary\r\nContent-Disposition: form-data; name="file"; filename="aborted.bin"\r\n\r\n'
                return {"type": "http.request", "body": prefix + data, "more_body": True}
            waiting.set()
            if interruption is ClientDisconnect:
                return {"type": "http.disconnect"}
            await asyncio.Future()

        async def parse():
            request = Request({"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=test-boundary")]}, receive=receive)
            async with multipart_upload.form(request, vault):
                pytest.fail("an interrupted multipart request produced a form")

        task = asyncio.create_task(parse())
        await asyncio.wait_for(waiting.wait(), timeout=5)
        if interruption is asyncio.CancelledError:
            task.cancel()
        with pytest.raises(interruption):
            await task

    asyncio.run(send())
    assert disk_writes == [len(data)]
    assert len(spools) == 1 and spools[0].closed


def test_cancel_during_rollover_closes_the_disk_file(vault, monkeypatch, observed_spools):
    release_rollover = Event()
    open_tempfile = tempfile.TemporaryFile
    disk_files = []

    async def send():
        loop = asyncio.get_running_loop()
        rolling = asyncio.Event()
        write_cancelled = asyncio.Event()

        def pause_rollover(*args, **kwargs):
            loop.call_soon_threadsafe(rolling.set)
            assert release_rollover.wait(5)
            disk_file = open_tempfile(*args, **kwargs)
            disk_files.append(disk_file)
            return disk_file

        async def worker(function, *args):
            try:
                return await asyncio.to_thread(function, *args)
            except asyncio.CancelledError:
                write_cancelled.set()
                raise

        monkeypatch.setattr(tempfile, "TemporaryFile", pause_rollover)
        monkeypatch.setattr(multipart_upload, "run_in_threadpool", worker)
        boundary = b'test-boundary'
        prefix = b'--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="aborted.bin"\r\n\r\n'
        data = b"x" * (formparsers.MultiPartParser.spool_max_size + 64 * 1024)

        async def receive():
            return {"type": "http.request", "body": prefix + data, "more_body": False}

        async def parse():
            request = Request({"type": "http", "headers": [(b"content-type", b"multipart/form-data; boundary=" + boundary)]}, receive=receive)
            async with multipart_upload.form(request, vault):
                pytest.fail("a cancelled multipart request produced a form")

        task = asyncio.create_task(parse())
        try:
            await asyncio.wait_for(rolling.wait(), timeout=5)
            task.cancel()
            await asyncio.wait_for(write_cancelled.wait(), timeout=5)
        finally:
            release_rollover.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(send())
    try:
        assert len(disk_files) == 1 and disk_files[0].closed
    finally:
        for disk_file in disk_files:
            disk_file.close()
