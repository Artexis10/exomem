"""Fence disk-backed HTTP upload spools on the vault's volume."""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

from anyio import CancelScope
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException, MultiPartParser, parse_options_header
from starlette.requests import Request

from . import media_capacity


class _UploadFile(UploadFile):
    def __init__(self, upload: UploadFile, vault: Path) -> None:
        super().__init__(upload.file, size=upload.size, filename=upload.filename, headers=upload.headers)
        self._vault = vault
        self._write_lock = Lock()

    async def write(self, data: bytes) -> None:
        if not data:
            return
        if not self._will_roll(len(data)):
            await super().write(data)
            return
        await run_in_threadpool(self._write_disk, data)
        if self.size is not None:
            self.size += len(data)

    def _write_disk(self, data: bytes) -> None:
        with self._write_lock:
            if self.file.closed:
                return
            rolling = self._in_memory
            # Starlette leaves the spool's directory at the tempfile default. After
            # rollover the open file identifies the device even if its path changes.
            device = os.stat(tempfile.gettempdir()).st_dev if rolling else os.fstat(self.file.fileno()).st_dev
            media_capacity.append_disk_spool(self._vault, self.file, data, device=device, rolling=rolling)

    async def close(self) -> None:
        if self._in_memory and self._write_lock.acquire(blocking=False):
            try:
                self.file.close()
            finally:
                self._write_lock.release()
        else:
            await run_in_threadpool(self._close_disk)

    def _close_disk(self) -> None:
        # A cancelled await can leave the worker creating the disk file.
        with self._write_lock:
            self.file.close()


class _Parser(MultiPartParser):
    def __init__(self, request: Request, vault: Path, **limits: int) -> None:
        super().__init__(request.headers, request.stream(), **limits)
        self._vault = vault
        self.uploads: list[_UploadFile] = []

    def on_headers_finished(self) -> None:
        # Starlette 1.3.1 installs a new UploadFile here and writes through it
        # after each received chunk. Keep its part parsing and spool ownership.
        super().on_headers_finished()
        if self._current_part.file is not None:
            upload = _UploadFile(self._current_part.file, self._vault)
            self._current_part.file = upload
            self.uploads.append(upload)


@asynccontextmanager
async def form(request: Request, vault: Path, **limits: int) -> AsyncIterator[FormData]:
    """Parse and close one form using Starlette's multipart limits and error mapping."""
    content_type = request.headers.get("Content-Type")
    if parse_options_header is None or parse_options_header(content_type)[0] != b"multipart/form-data":
        async with request.form(**limits) as parsed:
            yield parsed
        return

    parser = _Parser(request, vault, **limits)
    try:
        try:
            parsed = await parser.parse()
        except MultiPartException as exc:
            if "app" in request.scope:
                raise HTTPException(status_code=400, detail=exc.message) from exc
            raise
        yield parsed
    finally:
        # Starlette closes partial files for multipart errors and OSError only.
        with CancelScope(shield=True):
            for upload in parser.uploads:
                await upload.close()
