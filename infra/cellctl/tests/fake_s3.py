"""S3 at the SDK boundary, shared by the tests that reach cellctl's staging bucket."""
from __future__ import annotations

import hashlib
import itertools
from urllib.parse import parse_qs, urlsplit

from botocore.exceptions import ClientError


class FakeS3:
    """S3 at the SDK boundary. One store; every client is cellctl's one staging key (D9)."""

    def __init__(self) -> None:
        self.uploads: dict[str, dict] = {}
        self.versions: dict[str, list[tuple[str, bytes]]] = {}
        self.calls: list[tuple[str, str]] = []  # (key id, object name)
        self.abort_error: Exception | None = None
        self._ids = itertools.count(1)

    def client(self, cluster_config, key_id, key_secret) -> FakeS3Client:
        return FakeS3Client(self, key_id)

    def put_part(self, url: str, data: bytes) -> str:
        """What the export Job's PUT through a presigned part URL does."""
        query = {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}
        etag = '"' + hashlib.md5(data).hexdigest() + '"'  # noqa: S324 - S3's part ETag
        self.uploads[query["upload_id"]]["parts"][int(query["part"])] = (etag, data)
        return etag


class FakeS3Client:
    def __init__(self, store: FakeS3, key_id: str) -> None:
        self.store, self.key_id = store, key_id

    def _use(self, name: str) -> None:
        self.store.calls.append((self.key_id, name))

    def get_paginator(self, operation: str):
        return type("Paginator", (), {"paginate": lambda _self, **kwargs: [getattr(self, operation)(**kwargs)]})()

    def list_multipart_uploads(self, Bucket, Prefix):
        self._use(Prefix)
        return {"Uploads": [{"Key": upload["key"], "UploadId": upload_id}
                            for upload_id, upload in self.store.uploads.items() if upload["key"].startswith(Prefix)]}

    def list_object_versions(self, Bucket, Prefix):
        self._use(Prefix)
        return {"Versions": [{"Key": key, "VersionId": version} for key, versions in self.store.versions.items()
                             if key.startswith(Prefix) for version, _data in versions]}

    def list_parts(self, Bucket, Key, UploadId):
        self._use(Key)
        if UploadId not in self.store.uploads:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        parts = self.store.uploads[UploadId]["parts"]
        return {"Parts": [{"PartNumber": number, "ETag": parts[number][0], "Size": len(parts[number][1])}
                          for number in sorted(parts)]}

    def create_multipart_upload(self, Bucket, Key, **_headers):
        self._use(Key)
        upload_id = f"upload-{next(self.store._ids)}"
        self.store.uploads[upload_id] = {"key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def generate_presigned_url(self, operation, Params, ExpiresIn, HttpMethod=None):
        self._use(Params["Key"])
        query = {"op": operation, "key_id": self.key_id, "expires": ExpiresIn, "upload_id": Params.get("UploadId"),
                 "part": Params.get("PartNumber"), "version": Params.get("VersionId")}
        return f"https://storage.test/{Params['Bucket']}/{Params['Key']}?" + "&".join(
            f"{name}={value}" for name, value in query.items() if value is not None)

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        self._use(Key)
        upload = self.store.uploads.pop(UploadId)
        data = b"".join(upload["parts"][part["PartNumber"]][1] for part in MultipartUpload["Parts"])
        version = f"version-{next(self.store._ids)}"
        self.store.versions.setdefault(Key, []).append((version, data))
        return {"VersionId": version}

    def head_object(self, Bucket, Key, VersionId=None):
        self._use(Key)
        return {"ContentLength": len(dict(self.store.versions[Key])[VersionId]), "VersionId": VersionId}

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self._use(Key)
        if self.store.abort_error is not None:
            raise self.store.abort_error
        self.store.uploads.pop(UploadId, None)

    def delete_object(self, Bucket, Key, VersionId):
        self._use(Key)
        self.store.versions[Key] = [entry for entry in self.store.versions[Key] if entry[0] != VersionId]
        if not self.store.versions[Key]:
            del self.store.versions[Key]
