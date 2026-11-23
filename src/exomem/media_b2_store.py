"""Exact encrypted replica versions in a declared Backblaze B2 bucket and prefix."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack, aclosing, contextmanager
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote

import httpx

from . import media_edge_b2 as native
from .media_store import (
    MediaStoreError,
    ObjectHead,
    ObjectTooLarge,
    ReplicaAbsent,
    StreamReader,
    before_deadline,
)
from .originals import is_sha256

_BLOCK = 1024 * 1024
_MAX_PART_BYTES = 64 * 1024 * 1024
_UPLOAD_ATTEMPTS = 5  # With the native backoff, a busy pod gets about 15 seconds to recover.
_CREDENTIAL_BYTES = 4096


def _unavailable(reason: str) -> MediaStoreError:
    return MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"B2 replica {reason}")


@contextmanager
def _provider_operation():
    try:
        yield
    except (native.ProviderError, httpx.HTTPError, httpx.InvalidURL, OSError) as error:
        # Keep provider bodies, headers and URLs out of the store's error.
        raise _unavailable("operation could not be verified") from error


def _take(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) == size:
        return data
    received = bytearray(data)
    while len(received) < size and (data := stream.read(size - len(received))):
        received.extend(data)
    if len(received) != size:
        raise MediaStoreError("MEDIA_UPLOAD_MISMATCH", "the ciphertext ended before its declared size")
    return bytes(received)


def _check_size_and_digest(stream: BinaryIO, actual: str, declared: str) -> None:
    if stream.read(1) or actual != declared:
        raise MediaStoreError("MEDIA_UPLOAD_MISMATCH", "the ciphertext differs from its declared size or digest")


class B2ReplicaStore:
    def __init__(self, scope: native.BucketScope, credential_file: Path):
        self.scope = scope
        self._credential_file = credential_file

    @classmethod
    def from_configuration(cls, config: Mapping[str, object]) -> B2ReplicaStore:
        bucket, bucket_id = config.get("bucket"), config.get("bucket_id")
        prefix, credential = config.get("prefix"), config.get("credential_file")
        # The bucket becomes one URL segment. The native object name can occupy at most 1024 UTF-8 bytes.
        if (not isinstance(bucket, str) or not bucket or
                not all(c.isascii() and (c.isalnum() or c == "-") for c in bucket)
                or not isinstance(bucket_id, str) or not bucket_id.strip()):
            raise _unavailable("requires a bucket name and ID")
        if (not isinstance(prefix, str) or not prefix or len(prefix.encode()) + 64 > 1024
                or any(ord(c) < 32 or ord(c) == 127 for c in prefix)
                or any(part in (".", "..") for part in prefix.split("/"))):
            raise _unavailable("requires an exact object prefix")
        if not isinstance(credential, str) or "\0" in credential or not Path(credential).is_absolute():
            raise _unavailable("requires an absolute credential file path")
        return cls(native.BucketScope(bucket_id, bucket, prefix), Path(credential))

    def _credential(self) -> tuple[str, str]:
        try:
            with self._credential_file.open("rb") as source:
                raw = source.read(_CREDENTIAL_BYTES + 1)
            if len(raw) > _CREDENTIAL_BYTES:
                raise ValueError("credential size")
            credential = json.loads(raw)
            key_id, key = credential["key_id"], credential["application_key"]
            if (not all(isinstance(value, str) and value and all(33 <= ord(c) <= 126 for c in value)
                        for value in (key_id, key)) or ":" in key_id):
                raise ValueError("credential shape")
            return key_id, key
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise _unavailable("credential file is unreadable or invalid") from None

    @contextmanager
    def _operation(self, *, deadline: float | None = None):
        if deadline is not None and time.monotonic() >= deadline:
            raise _unavailable("operation exceeded its deadline")
        key_id, key = self._credential()
        with _provider_operation(), asyncio.Runner() as runner:
            held = AsyncExitStack()

            def run(work):
                return runner.run(work if deadline is None else before_deadline(deadline, work))

            try:
                client = run(held.enter_async_context(httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=5.0), trust_env=False,
                )))
                yield native.B2(client, key_id=key_id, application_key=key, attempts=_UPLOAD_ATTEMPTS,
                                bucket=self.scope.bucket_name, scope=self.scope), run, held
            finally:
                runner.run(held.aclose())

    def require_identity(self, *, deadline: float | None = None) -> None:
        # A reachability record must come from the provider now, not from a cached authorization.
        with self._operation(deadline=deadline) as (provider, run, _):
            run(provider.verify_identity())

    def _key(self, name: str) -> str:
        if not is_sha256(name):
            raise ValueError("invalid B2 replica object")
        return self.scope.prefix + name

    def _exact_head(self, info: native.FileInfo, key: str, version: str) -> ObjectHead:
        digest = info.info.get(native.SHA256_INFO)
        if (info.name != key or info.file_id != version or info.bucket_id != self.scope.bucket_id
                or info.action != "upload" or not is_sha256(digest)):
            raise _unavailable("metadata did not identify the exact completed version")
        return ObjectHead(info.size, digest)

    def head(self, name: str, version: str, *, deadline: float | None = None) -> ObjectHead | None:
        key = self._key(name)
        with self._operation(deadline=deadline) as (provider, run, _):
            info = run(provider.file_info(version))
        return self._exact_head(info, key, version) if info is not None else None

    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str:
        key = self._key(name)
        if not is_sha256(sha256) or size < 0:
            raise ValueError("invalid B2 replica upload declaration")
        if size > _MAX_PART_BYTES * native.MAX_PARTS:
            raise ObjectTooLarge(size, _MAX_PART_BYTES * native.MAX_PARTS)
        with self._operation() as (provider, run, _):
            part = max(native.DEFAULT_PART_BYTES, run(provider.minimum_part_bytes()), -(-size // native.MAX_PARTS))
            if size <= min(part, _MAX_PART_BYTES):
                work = self._upload_small(provider, key, stream, size, sha256)
            else:
                if part > _MAX_PART_BYTES:
                    raise _unavailable("requires parts above the transfer buffer limit")
                work = self._upload_large(provider, key, stream, size, sha256, part)
            return run(work)

    async def _upload_small(
        self, provider: native.B2, key: str, stream: BinaryIO, size: int, sha256: str,
    ) -> str:
        data = _take(stream, size)
        _check_size_and_digest(stream, hashlib.sha256(data).hexdigest(), sha256)
        nonce = uuid.uuid4().hex
        for attempt in range(_UPLOAD_ATTEMPTS):
            try:
                target = await provider.upload_target()
                version = await provider.upload_file(
                    target, key, data, hashlib.sha1(data).hexdigest(),
                    {native.SHA256_INFO: sha256, native.NONCE_INFO: nonce},
                )
                break
            except native.ProviderError as error:
                if isinstance(error, native.ProviderUncertain):
                    # Only this attempt's nonce proves that its bytes landed.
                    head = await provider.head_name(key)
                    if (head is not None and head.name == key and head.size == size
                            and head.info.get(native.SHA256_INFO) == sha256 and head.info.get(native.NONCE_INFO) == nonce):
                        version = head.file_id
                        break
                if attempt + 1 == _UPLOAD_ATTEMPTS or not native.upload_retryable(error):
                    raise
                await asyncio.sleep(native.upload_retry_delay(attempt, error))
        await self._confirm_upload(provider, key, version, size, sha256, nonce=nonce)
        return version

    async def _upload_large(
        self, provider: native.B2, key: str, stream: BinaryIO, size: int, sha256: str, part: int,
    ) -> str:
        version = await provider.start_large(key, {native.SHA256_INFO: sha256})
        hashes = []
        digest = hashlib.sha256()
        for offset in range(0, size, part):
            data = _take(stream, min(part, size - offset))
            digest.update(data)
            wire = hashlib.sha1(data).hexdigest()
            await provider.upload_part(version, len(hashes) + 1, data, wire)
            hashes.append(wire)
            del data
        _check_size_and_digest(stream, digest.hexdigest(), sha256)
        try:
            await provider.finish_large(version, hashes)
        except native.ProviderUncertain:
            pass  # The exact metadata read decides whether this file ID completed.
        await self._confirm_upload(provider, key, version, size, sha256)
        return version

    async def _confirm_upload(
        self, provider: native.B2, key: str, version: str, size: int, sha256: str, *, nonce: str | None = None,
    ) -> None:
        info = await provider.file_info(version)
        if (info is None or self._exact_head(info, key, version) != ObjectHead(size, sha256)
                or (nonce is not None and info.info.get(native.NONCE_INFO) != nonce)):
            raise _unavailable("upload did not confirm its exact ciphertext")

    def open(
        self, name: str, version: str, *, expected_size: int | None = None, deadline: float | None = None,
    ):
        return self._open(name, version, expected_size, deadline=deadline)

    @contextmanager
    def _open(self, name: str, version: str, size: int | None, *, deadline: float | None) -> Iterator[StreamReader]:
        key = self._key(name)
        with self._operation(deadline=deadline) as (provider, run, held):
            response = run(held.enter_async_context(provider.download(version)))
            if response.status_code == 404 and run(native.download_is_missing(response)):
                raise ReplicaAbsent()
            length = response.headers.get("content-length", "")
            if (response.headers.get("x-bz-file-id") != version or
                    unquote(response.headers.get("x-bz-file-name", "")) != key or
                    not length.isascii() or not length.isdigit() or len(length) > 20):
                raise _unavailable("download did not identify the exact version and length")
            received = int(length)
            if (response.status_code != 200 or "content-range" in response.headers
                    or (size is not None and received != size)):
                raise _unavailable("download did not return the full declared version")
            chunks = native.stream_body(response, _BLOCK)
            run(held.enter_async_context(aclosing(chunks)))

            def body() -> Iterator[bytes]:
                remaining = received
                with _provider_operation():
                    while (data := run(anext(chunks, None))) is not None:
                        remaining -= len(data)
                        if remaining < 0:
                            raise _unavailable("download exceeded its response length")
                        yield data
                if remaining:
                    raise _unavailable("download ended before its response length")

            yield StreamReader(body())
