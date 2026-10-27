"""An in-process Backblaze B2 native API (v3) at the HTTP level, for the media edge tests.

It answers real httpx requests through a mock transport. It enforces the rules the
edge depends on: SHA-1 on every upload, token use, two or more parts with a
minimum size for all but the last, and a finish that is not repeatable. It also
records every call, and it can lose a reply, with or without the call having taken effect.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from urllib.parse import parse_qs, quote, unquote

import httpx

API = "https://api000.fake-b2.test"
DOWNLOAD = "https://dl000.fake-b2.test"
UPLOAD = "https://up000.fake-b2.test"


class FakeB2:
    def __init__(self, *, bucket: str = "media-bucket", minimum_part: int = 1000, shape: str = "single"):
        self.bucket, self.bucket_id = bucket, "bucket-id-1"
        self.minimum_part, self.shape = minimum_part, shape
        self.files: dict[str, dict] = {}
        self.calls: list[str] = []
        self.upload_bytes = 0
        self.outage = False
        self._lost: dict[str, tuple[str, bool]] = {}  # endpoint -> (mode, whether an outage follows)
        self._parked: dict[str, dict] = {}
        self._counter = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def lose(self, endpoint: str, mode: str, then_outage: bool = False) -> None:
        """The next call to `endpoint` gets no reply.

        `reply`: the call took effect. `request`: it never took effect.
        `late`: it has not taken effect yet, and `land_late` applies it.
        """
        assert mode in {"reply", "request", "late"}
        self._lost[endpoint] = (mode, then_outage)

    def land_late(self) -> None:
        """Apply every parked write, as a slow provider would."""
        self.files.update(self._parked)
        self._parked.clear()

    def names(self, action: str = "upload") -> list[str]:
        return [item["name"] for item in self.files.values() if item["action"] == action]

    def version_of(self, name: str) -> str:
        return next(file_id for file_id, item in self.files.items() if item["name"] == name and item["action"] == "upload")

    def _id(self) -> str:
        self._counter += 1
        return f"4_zfile{self._counter:04d}"

    def _json(self, status: int, payload: dict) -> httpx.Response:
        return httpx.Response(status, json=payload)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.outage:
            raise httpx.ConnectError("the fake provider is down", request=request)
        before = copy.deepcopy(self.files) if self._lost else None
        endpoint, response = await self._dispatch(request)
        self.calls.append(endpoint)
        if endpoint in self._lost:
            mode, then_outage = self._lost.pop(endpoint)
            if mode != "reply":
                if mode == "late":
                    self._parked |= {file_id: item for file_id, item in self.files.items()
                                     if before.get(file_id) != item}
                self.files = before
            self.outage = then_outage
            raise httpx.ReadTimeout("the reply was lost", request=request)
        return response

    async def _dispatch(self, request: httpx.Request) -> tuple[str, httpx.Response]:
        url = request.url
        if url.path.endswith("/b2_authorize_account"):
            return "authorize", self._authorize(request)
        if request.headers.get("authorization", "") != "token-account" and url.host != "up000.fake-b2.test":
            return "unauthorized", self._json(401, {"code": "bad_auth_token"})
        if url.host == "up000.fake-b2.test":
            return await self._upload(request)
        if url.host == "dl000.fake-b2.test":
            return self._download(request)
        endpoint = url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if request.content else {}
        handler = getattr(self, f"_api_{endpoint}", None)
        if handler is None:
            return f"unsupported:{endpoint}", self._json(400, {"code": "unsupported"})
        return endpoint, handler(payload)

    def _authorize(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization", "").startswith("Basic "):
            storage = {"apiUrl": API, "downloadUrl": DOWNLOAD, "absoluteMinimumPartSize": self.minimum_part,
                       "recommendedPartSize": 100_000_000, "capabilities": ["readFiles", "writeFiles"]}
            bucket = {"id": self.bucket_id, "name": self.bucket}
            if self.shape == "single":
                storage |= {"bucketId": self.bucket_id, "bucketName": self.bucket}
            elif self.shape == "listed":
                storage |= {"bucketId": None, "bucketName": None, "allowed": {"buckets": [bucket]}}
            else:
                storage |= {"bucketId": None, "bucketName": None}
            return self._json(200, {"accountId": "account", "authorizationToken": "token-account",
                                    "apiInfo": {"storageApi": storage}})
        return self._json(401, {"code": "unauthorized"})

    def _api_b2_get_upload_url(self, payload: dict) -> httpx.Response:
        return self._json(200, {"bucketId": payload["bucketId"], "uploadUrl": f"{UPLOAD}/file", "authorizationToken": "token-small"})

    def _api_b2_start_large_file(self, payload: dict) -> httpx.Response:
        file_id = self._id()
        self.files[file_id] = {"name": payload["fileName"], "action": "start", "parts": {},
                               "info": payload.get("fileInfo", {}), "data": None}
        return self._json(200, {"fileId": file_id, "fileName": payload["fileName"], "action": "start"})

    def _api_b2_get_upload_part_url(self, payload: dict) -> httpx.Response:
        file_id = payload["fileId"]
        if self.files.get(file_id, {}).get("action") != "start":
            return self._json(400, {"code": "bad_request"})
        return self._json(200, {"fileId": file_id, "uploadUrl": f"{UPLOAD}/part/{file_id}", "authorizationToken": "token-part"})

    def _api_b2_finish_large_file(self, payload: dict) -> httpx.Response:
        item = self.files.get(payload["fileId"])
        if item is None or item["action"] != "start":
            return self._json(400, {"code": "bad_request", "message": "not an unfinished large file"})
        numbers = sorted(item["parts"])
        parts = [item["parts"][number] for number in numbers]
        valid = (
            numbers == list(range(1, len(parts) + 1))
            and len(parts) >= 2
            and all(len(data) >= self.minimum_part for data in parts[:-1])
            and payload["partSha1Array"] == [hashlib.sha1(data).hexdigest() for data in parts]
        )
        if not valid:
            return self._json(400, {"code": "bad_request", "message": "the parts do not match"})
        item["data"], item["action"] = b"".join(parts), "upload"
        return self._json(200, {"fileId": payload["fileId"], "fileName": item["name"], "action": "upload"})

    def _api_b2_cancel_large_file(self, payload: dict) -> httpx.Response:
        item = self.files.get(payload["fileId"])
        if item is None or item["action"] != "start":
            return self._json(400, {"code": "bad_request"})
        del self.files[payload["fileId"]]
        return self._json(200, {"fileId": payload["fileId"]})

    def _api_b2_get_file_info(self, payload: dict) -> httpx.Response:
        item = self.files.get(payload["fileId"])
        if item is None:
            return self._json(404, {"code": "file_not_present"})
        size = len(item["data"]) if item["data"] is not None else 0
        return self._json(200, {"fileId": payload["fileId"], "fileName": item["name"], "action": item["action"],
                                "contentLength": size, "fileInfo": item["info"]})

    async def _upload(self, request: httpx.Request) -> tuple[str, httpx.Response]:
        data = await request.aread()
        self.upload_bytes += len(data)
        sha1 = hashlib.sha1(data).hexdigest()
        if request.headers.get("x-bz-content-sha1") != sha1:
            return "upload", self._json(400, {"code": "bad_request", "message": "sha1 mismatch"})
        if request.url.path.startswith("/part/"):
            item = self.files.get(request.url.path.removeprefix("/part/"))
            if item is None or item["action"] != "start" or request.headers.get("authorization") != "token-part":
                return "upload_part", self._json(400, {"code": "bad_request"})
            item["parts"][int(request.headers["x-bz-part-number"])] = data
            return "upload_part", self._json(200, {"partNumber": int(request.headers["x-bz-part-number"])})
        if request.headers.get("authorization") != "token-small":
            return "upload_file", self._json(401, {"code": "bad_auth_token"})
        info = {key[len("x-bz-info-"):]: unquote(value) for key, value in request.headers.items()
                if key.startswith("x-bz-info-")}
        file_id = self._id()
        self.files[file_id] = {"name": unquote(request.headers["x-bz-file-name"]), "action": "upload",
                               "parts": {}, "info": info, "data": data}
        return "upload_file", self._json(200, {"fileId": file_id, "fileName": self.files[file_id]["name"],
                                               "contentLength": len(data), "fileInfo": info})

    def _download(self, request: httpx.Request) -> tuple[str, httpx.Response]:
        path = request.url.path
        if path.endswith("/b2_download_file_by_id"):
            item = self.files.get(parse_qs(request.url.query.decode())["fileId"][0])
            endpoint = "download"
        else:
            name = unquote(path.removeprefix(f"/file/{self.bucket}/"))
            found = [(file_id, item) for file_id, item in self.files.items()
                     if item["name"] == name and item["action"] == "upload"]
            item = found[-1][1] if found else None
            endpoint = "head_name"
        if item is None or item["action"] != "upload":
            return endpoint, self._json(404, {"code": "not_found"})
        data = item["data"]
        file_id = next(key for key, value in self.files.items() if value is item)
        headers = {"x-bz-file-id": file_id, "x-bz-file-name": quote(item["name"], safe="/"),
                   **{f"x-bz-info-{key}": quote(value, safe="") for key, value in item["info"].items()}}
        status, body = 200, data
        requested = request.headers.get("range")
        if requested and endpoint == "download":
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            start = int(match.group(1)) if match else len(data)
            if start >= len(data):
                return endpoint, httpx.Response(416, headers={"content-range": f"bytes */{len(data)}"})
            end = min(int(match.group(2)), len(data) - 1) if match and match.group(2) else len(data) - 1
            status, body = 206, data[start:end + 1]
            headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
        if request.method == "HEAD":
            return endpoint, httpx.Response(200, headers={**headers, "content-length": str(len(data))})
        # An unread stream, as a network response is: the edge streams it block by block.
        return endpoint, httpx.Response(status, headers={**headers, "content-length": str(len(body))},
                                        stream=httpx.ByteStream(body))
