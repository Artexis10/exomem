"""Backblaze B2 native API (v4) for media storage, over httpx.

Only the calls the edge needs. The edge key lacks `deleteFiles` and `listFiles`, and
no call here deletes a version. The hide serves the collector's `condemn` and the
purge of a deleted cell. Only the purge lists versions, with a second, list-only key
(automate-cell-storage-deletion D3). A rejected call did not take effect; an
uncertain one may have. The write paths in `media_edge` depend on that split.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import aclosing, asynccontextmanager, contextmanager
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx

# B2 refuses a key limited to a list of buckets, the kind Terraform creates, on API versions 2 and 3.
_API = "b2api/v4"
AUTHORIZE_URL = f"https://api.backblazeb2.com/{_API}/b2_authorize_account"
_INFO_HEADER = "x-bz-info-"
_OCTET_STREAM = "application/octet-stream"
SHA256_INFO = "exomem_sha256"
NONCE_INFO = "exomem_nonce"
# The native API limits a large file to 10,000 parts.
MAX_PARTS = 10_000
DEFAULT_PART_BYTES = 8 * 1024 * 1024
_ERROR_BYTES = 4096


# A listing that needs more pages than this fails, never stops short: 2.5 million versions, as the key
# watcher's own listing.
_MAX_LIST_PAGES = 2500


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, *, retry_after: float | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(message)


class ProviderRejected(ProviderError):
    """The provider refused the call before acting on it."""

    def __init__(self, message: str, status: int | None = None, *, missing: bool = False,
                 retry_after: float | None = None):
        self.missing = missing
        super().__init__(message, status, retry_after=retry_after)


class ProviderUncertain(ProviderError):
    """No answer or a server fault: the call may have taken effect."""


@dataclass(frozen=True, slots=True)
class FileInfo:
    file_id: str
    name: str
    size: int
    action: str
    info: Mapping[str, str]
    bucket_id: str | None = None


@dataclass(frozen=True, slots=True)
class UploadTarget:
    url: str
    token: str


# Native B2 capabilities that can remove a stored version directly, through a lifecycle rule or a retention
# bypass, or mint a key that can. A replica key holds none of them. Harmless bucket reads stay allowed,
# because keys made in the provider's console carry several.
_VERSION_REMOVING = ("deleteFiles", "deleteBuckets", "deleteKeys", "writeKeys", "writeBuckets",
                     "writeBucketLifecycleRules", "bypassGovernance", "writeFileRetentions")


@dataclass(frozen=True, slots=True)
class BucketScope:
    bucket_id: str
    bucket_name: str
    prefix: str

    def require(self, allowed: Mapping) -> None:
        # These names are capabilities defined by the native B2 authorization protocol.
        capabilities = allowed["capabilities"]
        buckets = allowed["buckets"]
        if (not isinstance(buckets, list) or len(buckets) != 1
                or buckets[0]["id"] != self.bucket_id or buckets[0]["name"] != self.bucket_name
                or allowed["namePrefix"] != self.prefix or not isinstance(capabilities, list)
                or "readFiles" not in capabilities or "writeFiles" not in capabilities
                or any(capability in capabilities for capability in _VERSION_REMOVING)):
            raise ProviderRejected("the application key does not have the declared replica scope")


@dataclass(frozen=True, slots=True)
class _Session:
    api_url: str
    download_url: str
    token: str
    bucket_id: str
    bucket_name: str
    minimum_part_bytes: int


_SessionKey = tuple[str, BucketScope, bytes]
_sessions: OrderedDict[_SessionKey, tuple[float, _Session]] = OrderedDict()
_sessions_lock = threading.Lock()
# Tokens are valid for at most 24 hours. Refresh this small process cache hourly or on a 401.
_SESSION_SECONDS = 3600
_SESSION_LIMIT = 32


def missing_reply(status: int, body: bytes) -> bool:
    """Identify a native B2 absence envelope, as opposed to an intermediary's HTTP status."""
    if status != 404 or len(body) > _ERROR_BYTES:
        return False
    try:
        data = json.loads(body)
        return type(data["status"]) is int and data["status"] == 404 and data["code"] == "not_found"
    except (KeyError, ValueError, TypeError, RecursionError):
        return False


def _classify(response: httpx.Response, *, identify_missing: bool = False) -> ProviderError:
    # 408 and 5xx can follow partial work; other statuses mean the call was refused.
    status = response.status_code
    message = f"the provider answered {status}"
    after = response.headers.get("Retry-After", "")
    retry_after = float(after) if after.isascii() and after.isdigit() else None
    if status >= 500 or status == 408:
        return ProviderUncertain(message, status, retry_after=retry_after)
    return ProviderRejected(message, status, missing=identify_missing and missing_reply(status, response.content),
                            retry_after=retry_after)


# Backblaze answers a busy upload pod with 503 and asks for a fresh upload URL after a growing pause.
UPLOAD_RETRY_SECONDS = 1.0
UPLOAD_RETRY_CAP_SECONDS = 16.0


def upload_retryable(error: ProviderError) -> bool:
    """Auth expiry, throttling and server faults recover with a fresh upload URL. A refused upload does not."""
    return not isinstance(error, ProviderRejected) or error.status in (401, 429)


def upload_retry_delay(attempt: int, error: ProviderError) -> float:
    return min(UPLOAD_RETRY_CAP_SECONDS, max(error.retry_after or 0.0, UPLOAD_RETRY_SECONDS * 2**attempt))


@contextmanager
def _shape():
    try:
        yield
    except (KeyError, IndexError, TypeError, ValueError, RecursionError) as error:
        raise ProviderUncertain("the provider answered with an unexpected shape") from error


def _bucket(storage: Mapping, name: str) -> tuple[str, str]:
    """Find the configured bucket among the buckets that the key is limited to."""
    for item in storage["allowed"]["buckets"] or []:
        if isinstance(item, Mapping) and item.get("name") == name and item.get("id"):
            return str(item["id"]), name
    raise ProviderRejected("the application key is not limited to the configured bucket")


def _info(headers: Mapping[str, str]) -> dict[str, str]:
    return {key[len(_INFO_HEADER):]: unquote(value) for key, value in headers.items()
            if key.lower().startswith(_INFO_HEADER)}


class B2:
    def __init__(self, client: httpx.AsyncClient, *, key_id: str, application_key: str,
                 bucket: str, attempts: int = 3, scope: BucketScope | None = None):
        self._client = client
        self._credentials = (key_id, application_key)
        self._bucket_name = bucket
        self._attempts = attempts
        self._scope = scope
        self._session_key = (
            (AUTHORIZE_URL, scope, hashlib.sha256(json.dumps(self._credentials).encode()).digest())
            if scope is not None else None
        )
        self._session: _Session | None = None

    def _expired(self) -> None:
        if self._session_key is not None:
            with _sessions_lock:
                cached = _sessions.get(self._session_key)
                if cached is not None and cached[1] is self._session:
                    del _sessions[self._session_key]
        self._session = None

    async def _authorized(self, *, fresh: bool = False) -> _Session:
        if self._session is not None and not fresh:
            return self._session
        if self._session_key is not None and not fresh:
            with _sessions_lock:
                cached = _sessions.get(self._session_key)
                if cached is not None and time.monotonic() < cached[0]:
                    _sessions.move_to_end(self._session_key)
                    self._session = cached[1]
                    return self._session
        try:
            response = await self._client.get(AUTHORIZE_URL, auth=self._credentials)
        except httpx.HTTPError as error:
            raise ProviderUncertain("the provider authorization failed") from error
        if response.status_code != 200:
            raise _classify(response)
        with _shape():
            body = response.json()
            storage = body["apiInfo"]["storageApi"]
            if self._scope is None:
                bucket_id, bucket_name = _bucket(storage, self._bucket_name)
            else:
                self._scope.require(storage["allowed"])
                bucket_id, bucket_name = self._scope.bucket_id, self._scope.bucket_name
                if (not all(isinstance(value, str) and value for value in
                            (storage["apiUrl"], storage["downloadUrl"], body["authorizationToken"]))
                        or type(storage["absoluteMinimumPartSize"]) is not int or storage["absoluteMinimumPartSize"] <= 0):
                    raise ValueError("invalid native authorization")
            self._session = _Session(storage["apiUrl"], storage["downloadUrl"], body["authorizationToken"],
                                     bucket_id, bucket_name, int(storage["absoluteMinimumPartSize"]))
        if self._session_key is not None:
            with _sessions_lock:
                _sessions[self._session_key] = (time.monotonic() + _SESSION_SECONDS, self._session)
                _sessions.move_to_end(self._session_key)
                if len(_sessions) > _SESSION_LIMIT:
                    _sessions.popitem(last=False)
        return self._session

    async def minimum_part_bytes(self) -> int:
        return (await self._authorized()).minimum_part_bytes

    async def verify_identity(self) -> None:
        """Authorize afresh, so the answer is a current observation of the provider and the key's scope."""
        await self._authorized(fresh=True)

    async def _call(self, endpoint: str, body: dict) -> httpx.Response:
        session = await self._authorized()
        try:
            return await self._client.post(f"{session.api_url}/{_API}/{endpoint}", json=body,
                                           headers={"Authorization": session.token})
        except httpx.HTTPError as error:
            raise ProviderUncertain(f"{endpoint} failed") from error

    async def _post(self, endpoint: str, body: dict) -> dict:
        response = await self._call(endpoint, body)
        if response.status_code == 401:
            self._expired()
            response = await self._call(endpoint, body)
        if response.status_code != 200:
            raise _classify(response, identify_missing=endpoint == "b2_get_file_info")
        with _shape():
            payload = response.json()
            if self._scope is not None and not isinstance(payload, dict):
                raise ValueError("invalid native response")
            return payload

    async def _upload(self, url: str, headers: dict[str, str], data: bytes) -> dict:
        try:
            response = await self._client.post(url, headers=headers, content=data)
        except httpx.HTTPError as error:
            raise ProviderUncertain("the provider upload failed") from error
        if response.status_code != 200:
            raise _classify(response)
        with _shape():
            return response.json()

    async def upload_target(self) -> UploadTarget:
        bucket_id = (await self._authorized()).bucket_id
        payload = await self._post("b2_get_upload_url", {"bucketId": bucket_id})
        with _shape():
            if self._scope is not None and payload.get("bucketId") != bucket_id:
                raise ProviderRejected("the upload target names another bucket")
            return UploadTarget(payload["uploadUrl"], payload["authorizationToken"])

    async def upload_file(self, target: UploadTarget, key: str, data: bytes, sha1: str,
                          info: Mapping[str, str]) -> str:
        headers = {"Authorization": target.token, "X-Bz-File-Name": quote(key, safe="/"),
                   "Content-Type": _OCTET_STREAM, "X-Bz-Content-Sha1": sha1,
                   **{f"X-Bz-Info-{name}": quote(value, safe="") for name, value in info.items()}}
        payload = await self._upload(target.url, headers, data)
        with _shape():
            return str(payload["fileId"])

    async def start_large(self, key: str, info: Mapping[str, str]) -> str:
        bucket_id = (await self._authorized()).bucket_id
        payload = await self._post("b2_start_large_file", {
            "bucketId": bucket_id, "fileName": key,
            "contentType": _OCTET_STREAM, "fileInfo": dict(info)})
        with _shape():
            if self._scope is not None and (
                payload.get("bucketId") != bucket_id or payload.get("fileName") != key or payload.get("action") != "start"
                or not isinstance(payload.get("fileId"), str) or not payload["fileId"]
            ):
                raise ProviderRejected("the multipart start did not identify the declared version")
            return str(payload["fileId"])

    async def upload_part(self, file_id: str, number: int, data: bytes, sha1: str) -> None:
        """Send one part. A part is replaced by number, so a failed attempt may repeat."""
        for attempt in range(self._attempts):
            try:
                target = await self._post("b2_get_upload_part_url", {"fileId": file_id})
                with _shape():
                    if self._scope is not None and target.get("fileId") != file_id:
                        raise ProviderRejected("the part upload target names another version")
                    url, token = target["uploadUrl"], target["authorizationToken"]
                await self._upload(url, {"Authorization": token, "X-Bz-Part-Number": str(number),
                                         "X-Bz-Content-Sha1": sha1}, data)
                return
            except ProviderError as error:
                if attempt + 1 == self._attempts or (self._scope is not None and not upload_retryable(error)):
                    raise
                if self._scope is not None:
                    await asyncio.sleep(upload_retry_delay(attempt, error))

    async def finish_large(self, file_id: str, part_sha1: list[str]) -> None:
        await self._post("b2_finish_large_file", {"fileId": file_id, "partSha1Array": part_sha1})

    async def cancel_large(self, file_id: str) -> None:
        await self._post("b2_cancel_large_file", {"fileId": file_id})

    async def hide_file(self, key: str) -> None:
        """Make the name's newest version non-current. The version stays readable by ID until the bucket rule runs."""
        await self._post("b2_hide_file", {"bucketId": (await self._authorized()).bucket_id, "fileName": key})

    async def list_versions(self, prefix: str) -> list[FileInfo]:
        """Every entry under the prefix, by name, and newest first within a name, as the provider lists them."""
        session = await self._authorized()
        entries: list[FileInfo] = []
        start: dict[str, str] = {}
        seen: set[tuple[str, str]] = set()
        for _ in range(_MAX_LIST_PAGES):
            payload = await self._post("b2_list_file_versions", {
                "bucketId": session.bucket_id, "prefix": prefix, "maxFileCount": 1000, **start})
            with _shape():
                entries += [FileInfo(str(item["fileId"]), str(item["fileName"]), int(item.get("contentLength") or 0),
                                     str(item["action"]), dict(item.get("fileInfo") or {})) for item in payload["files"]]
                if payload.get("nextFileName") is None:
                    return entries
                cursor = (str(payload["nextFileName"]), str(payload["nextFileId"]))
            if cursor in seen:
                raise ProviderUncertain("the provider repeated a listing cursor")
            seen.add(cursor)
            start = {"startFileName": cursor[0], "startFileId": cursor[1]}
        raise ProviderUncertain("the provider listing did not end")

    async def file_info(self, file_id: str) -> FileInfo | None:
        try:
            payload = await self._post("b2_get_file_info", {"fileId": file_id})
        except ProviderRejected as error:
            if error.missing or (self._scope is None and error.status == 404):
                return None
            raise
        with _shape():
            if self._scope is not None and (
                not all(isinstance(payload[field], str) and payload[field]
                        for field in ("fileId", "fileName", "bucketId", "action"))
                or type(payload["contentLength"]) is not int or payload["contentLength"] < 0
                or not isinstance(payload.get("fileInfo"), dict)
            ):
                raise ValueError("invalid native file metadata")
            return FileInfo(str(payload["fileId"]), str(payload["fileName"]), int(payload["contentLength"]),
                            str(payload["action"]), dict(payload.get("fileInfo") or {}), payload.get("bucketId"))

    async def _head(self, key: str) -> httpx.Response:
        session = await self._authorized()
        url = f"{session.download_url}/file/{session.bucket_name}/{quote(key, safe='/')}"
        try:
            return await self._client.head(url, headers={"Authorization": session.token})
        except httpx.HTTPError as error:
            raise ProviderUncertain("the provider HEAD failed") from error

    async def head_name(self, key: str) -> FileInfo | None:
        """The newest version of a name, by the provider's download-by-name HEAD. None when it is hidden or absent."""
        response = await self._head(key)
        if response.status_code == 401:
            self._expired()
            response = await self._head(key)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _classify(response)
        with _shape():
            return FileInfo(response.headers["x-bz-file-id"], unquote(response.headers["x-bz-file-name"]),
                            int(response.headers["content-length"]), "upload", _info(response.headers))

    async def _open(self, file_id: str, byte_range: str | None) -> httpx.Response:
        session = await self._authorized()
        headers = {"Authorization": session.token, "Accept-Encoding": "identity",
                   **({"Range": byte_range} if byte_range else {})}
        request = self._client.build_request(
            "GET", f"{session.download_url}/{_API}/b2_download_file_by_id",
            params={"fileId": file_id}, headers=headers)
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise ProviderUncertain("the provider download failed") from error

    @asynccontextmanager
    async def download(self, file_id: str, byte_range: str | None = None) -> AsyncIterator[httpx.Response]:
        """Open a ranged read of one version. The caller checks the status and the name."""
        response = await self._open(file_id, byte_range)
        if response.status_code == 401:
            await response.aclose()
            self._expired()
            response = await self._open(file_id, byte_range)
        try:
            yield response
        finally:
            await response.aclose()


async def stream_body(response: httpx.Response, block: int) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw(block):
            yield chunk
    except httpx.HTTPError as error:
        raise ProviderUncertain("the provider download broke off") from error


async def download_is_missing(response: httpx.Response) -> bool:
    async with aclosing(stream_body(response, _ERROR_BYTES + 1)) as chunks:
        return missing_reply(response.status_code, await anext(chunks, b""))
