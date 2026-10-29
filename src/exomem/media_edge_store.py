"""The cell's media store over the media edge (design D6).

The edge holds the only bucket credential. The cell holds a credential that
cellctl signs, and the edge decides from it which objects the cell may reach.
The cell reports every edge and provider fault under the store's own codes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

import httpx

from . import originals
from .media_edge_protocol import (
    ADMIT_ACTION,
    OBJECTS_PATH,
    SHA256_HEADER,
    SIZE_HEADER,
    STATUS_ABSENT,
    STATUS_MISMATCH,
    STATUS_TOO_LARGE,
)
from .media_store import MediaStoreError, ObjectHead, ObjectTooLarge, _declared_chunks

URL_ENV = "EXOMEM_MEDIA_EDGE_URL"
CREDENTIAL_FILE_ENV = "EXOMEM_MEDIA_EDGE_CREDENTIAL_FILE"
_BLOCK = 1024 * 1024


def _unavailable(reason: str) -> MediaStoreError:
    return MediaStoreError("MEDIA_STORE_UNAVAILABLE", reason)


@contextmanager
def _outage(operation: str):
    """A transport fault is an outage, which leaves offload demand retryable."""
    try:
        yield
    except (httpx.HTTPError, httpx.InvalidURL, OSError) as error:
        raise _unavailable(f"the media edge {operation} failed: {error}") from error


def _refusal(response: httpx.Response, size: int | None = None) -> MediaStoreError:
    if response.status_code == STATUS_MISMATCH:
        return MediaStoreError("MEDIA_UPLOAD_MISMATCH", "the media edge refused the uploaded bytes")
    if response.status_code == STATUS_TOO_LARGE and size is not None:
        try:
            return ObjectTooLarge(size, int(response.json()["bound"]))
        except (ValueError, KeyError, TypeError):
            pass  # an edge that does not state its bound is faulty, not a final refusal
    return _unavailable(f"the media edge answered {response.status_code}")


class _Reader:
    def __init__(self, response: httpx.Response):
        self._chunks = response.iter_raw(_BLOCK)
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        with _outage("read"):
            while size < 0 or len(self._buffer) < size:
                chunk = next(self._chunks, None)
                if chunk is None:
                    break
                self._buffer.extend(chunk)
        count = len(self._buffer) if size < 0 else size
        block = bytes(self._buffer[:count])
        del self._buffer[:count]
        return block


class EdgeStore:
    def __init__(self, url: str, credential_file: Path, *, connect_seconds: float, read_seconds: float):
        self._url = url
        self._credential_file = credential_file
        self._timeout = httpx.Timeout(read_seconds, connect=connect_seconds)

    @classmethod
    def from_configuration(cls, config: Mapping[str, object]) -> EdgeStore:
        url, credential = os.environ.get(URL_ENV), os.environ.get(CREDENTIAL_FILE_ENV)
        if not url or not credential:
            raise _unavailable("the media edge is not configured")
        return cls(url, Path(credential), connect_seconds=float(config.get("edge_connect_seconds", 5)),
                   read_seconds=float(config.get("edge_read_seconds", 60)))

    @contextmanager
    def _client(self, operation: str):
        """Read the credential for each operation, so a rotated one applies without a restart."""
        with _outage(operation):
            credential = self._credential_file.read_text(encoding="ascii").strip()
            if not credential:
                raise _unavailable("the media edge credential is empty")
            # An in-cluster literal address needs neither proxy nor DNS configuration.
            with httpx.Client(base_url=self._url, headers={"authorization": f"Bearer {credential}"},
                              timeout=self._timeout, trust_env=False) as client:
                yield client

    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str:
        if not originals.is_sha256(name) or not originals.is_sha256(sha256) or size < 0:
            raise ValueError("invalid upload declaration")
        declaration = {SIZE_HEADER: str(size), SHA256_HEADER: sha256}
        path = f"{OBJECTS_PATH}{name}"
        with self._client("upload") as client:
            # The edge refuses an oversized object here, before the cell sends any byte.
            admitted = client.post(f"{path}/{ADMIT_ACTION}", headers=declaration)
            if admitted.status_code != 204:
                raise _refusal(admitted, size)
            response = client.put(path, headers={**declaration, "content-length": str(size)},
                                  content=_declared_chunks(stream, size, sha256))
        if response.status_code != 201:
            raise _refusal(response, size)
        try:
            return str(response.json()["version"])
        except (ValueError, KeyError, TypeError) as error:
            raise _unavailable("the media edge answered without a version") from error

    def head(self, name: str, version: str) -> ObjectHead | None:
        with self._client("head") as client:
            response = client.head(f"{OBJECTS_PATH}{name}", params={"version": version})
        if response.status_code == STATUS_ABSENT:
            return None
        if response.status_code != 200:
            raise _refusal(response)
        try:
            return ObjectHead(int(response.headers[SIZE_HEADER]), response.headers[SHA256_HEADER])
        except (KeyError, ValueError) as error:
            raise _unavailable("the media edge answered without a size and digest") from error

    @contextmanager
    def open(self, name: str, version: str):
        with self._client("read") as client:
            with client.stream("GET", f"{OBJECTS_PATH}{name}", params={"version": version}) as response:
                if response.status_code != 200:
                    response.read()
                    raise _refusal(response)
                yield _Reader(response)
