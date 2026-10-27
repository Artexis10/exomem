"""Backblaze B2 native API (v3) for the media edge, over httpx.

Only the calls the edge needs. The edge key lacks `deleteFiles`, and no call here
hides, lists or deletes a version. A rejected call did not take effect; an
uncertain one may have. The write paths in `media_edge` depend on that split.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from urllib.parse import quote, unquote

import httpx

AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
_INFO_HEADER = "x-bz-info-"
_OCTET_STREAM = "application/octet-stream"


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        self.status = status
        super().__init__(message)


class ProviderRejected(ProviderError):
    """The provider refused the call before acting on it."""


class ProviderUncertain(ProviderError):
    """No answer or a server fault: the call may have taken effect."""


@dataclass(frozen=True, slots=True)
class FileInfo:
    file_id: str
    name: str
    size: int
    action: str
    info: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class UploadTarget:
    url: str
    token: str


@dataclass(frozen=True, slots=True)
class _Session:
    api_url: str
    download_url: str
    token: str
    bucket_id: str
    bucket_name: str
    minimum_part_bytes: int


def _classify(response: httpx.Response) -> ProviderError:
    # 408 and 5xx can follow partial work; other statuses mean the call was refused.
    uncertain = response.status_code >= 500 or response.status_code == 408
    kind = ProviderUncertain if uncertain else ProviderRejected
    return kind(f"the provider answered {response.status_code}", response.status_code)


@contextmanager
def _shape():
    try:
        yield
    except (KeyError, TypeError, ValueError) as error:
        raise ProviderUncertain("the provider answered with an unexpected shape") from error


def _bucket(storage: Mapping, name: str) -> tuple[str, str]:
    """Find the configured bucket among the buckets that the key is limited to."""
    reported = [{"id": storage.get("bucketId"), "name": storage.get("bucketName")}]
    allowed = storage.get("allowed")
    if isinstance(allowed, Mapping):
        reported += allowed.get("buckets") or []
    for item in reported:
        if isinstance(item, Mapping) and item.get("name") == name and item.get("id"):
            return str(item["id"]), name
    raise ProviderRejected("the application key is not limited to the configured bucket")


def _info(headers: Mapping[str, str]) -> dict[str, str]:
    return {key[len(_INFO_HEADER):]: unquote(value) for key, value in headers.items()
            if key.lower().startswith(_INFO_HEADER)}


class B2:
    def __init__(self, client: httpx.AsyncClient, *, key_id: str, application_key: str,
                 bucket: str, attempts: int = 3):
        self._client = client
        self._credentials = (key_id, application_key)
        self._bucket_name = bucket
        self._attempts = attempts
        self._session: _Session | None = None

    async def _authorized(self) -> _Session:
        if self._session is not None:
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
            bucket_id, bucket_name = _bucket(storage, self._bucket_name)
            self._session = _Session(storage["apiUrl"], storage["downloadUrl"], body["authorizationToken"],
                                     bucket_id, bucket_name, int(storage["absoluteMinimumPartSize"]))
        return self._session

    async def minimum_part_bytes(self) -> int:
        return (await self._authorized()).minimum_part_bytes

    async def _call(self, endpoint: str, body: dict) -> httpx.Response:
        session = await self._authorized()
        try:
            return await self._client.post(f"{session.api_url}/b2api/v3/{endpoint}", json=body,
                                           headers={"Authorization": session.token})
        except httpx.HTTPError as error:
            raise ProviderUncertain(f"{endpoint} failed") from error

    async def _post(self, endpoint: str, body: dict) -> dict:
        response = await self._call(endpoint, body)
        if response.status_code == 401:
            self._session = None  # the authorization expired; ask once more
            response = await self._call(endpoint, body)
        if response.status_code != 200:
            raise _classify(response)
        with _shape():
            return response.json()

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
        payload = await self._post("b2_get_upload_url", {"bucketId": (await self._authorized()).bucket_id})
        with _shape():
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
        payload = await self._post("b2_start_large_file", {
            "bucketId": (await self._authorized()).bucket_id, "fileName": key,
            "contentType": _OCTET_STREAM, "fileInfo": dict(info)})
        with _shape():
            return str(payload["fileId"])

    async def upload_part(self, file_id: str, number: int, data: bytes, sha1: str) -> None:
        """Send one part. A part is replaced by number, so a failed attempt may repeat."""
        for attempt in range(self._attempts):
            try:
                target = await self._post("b2_get_upload_part_url", {"fileId": file_id})
                with _shape():
                    url, token = target["uploadUrl"], target["authorizationToken"]
                await self._upload(url, {"Authorization": token, "X-Bz-Part-Number": str(number),
                                         "X-Bz-Content-Sha1": sha1}, data)
                return
            except ProviderError:
                if attempt + 1 == self._attempts:
                    raise

    async def finish_large(self, file_id: str, part_sha1: list[str]) -> None:
        await self._post("b2_finish_large_file", {"fileId": file_id, "partSha1Array": part_sha1})

    async def cancel_large(self, file_id: str) -> None:
        await self._post("b2_cancel_large_file", {"fileId": file_id})

    async def file_info(self, file_id: str) -> FileInfo | None:
        try:
            payload = await self._post("b2_get_file_info", {"fileId": file_id})
        except ProviderRejected as error:
            if error.status == 404:
                return None
            raise
        with _shape():
            return FileInfo(str(payload["fileId"]), str(payload["fileName"]), int(payload["contentLength"]),
                            str(payload["action"]), dict(payload.get("fileInfo") or {}))

    async def head_name(self, key: str) -> FileInfo | None:
        """The newest version of a name, by the provider's download-by-name HEAD."""
        session = await self._authorized()
        url = f"{session.download_url}/file/{session.bucket_name}/{quote(key, safe='/')}"
        try:
            response = await self._client.head(url, headers={"Authorization": session.token})
        except httpx.HTTPError as error:
            raise ProviderUncertain("the provider HEAD failed") from error
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise _classify(response)
        with _shape():
            return FileInfo(response.headers["x-bz-file-id"], unquote(response.headers["x-bz-file-name"]),
                            int(response.headers["content-length"]), "upload", _info(response.headers))

    async def _open(self, file_id: str, byte_range: str | None) -> httpx.Response:
        session = await self._authorized()
        headers = {"Authorization": session.token, **({"Range": byte_range} if byte_range else {})}
        request = self._client.build_request(
            "GET", f"{session.download_url}/b2api/v3/b2_download_file_by_id",
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
            self._session = None
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
