"""C6 export transport against a real S3-compatible store (MinIO standing in for B2).

Skipped unless CELLCTL_TEST_S3_ENDPOINT, CELLCTL_TEST_S3_ACCESS_KEY and
CELLCTL_TEST_S3_SECRET_KEY name a disposable store with SSE-S3 enabled
(MinIO: MINIO_KMS_SECRET_KEY). The fast suite's FakeS3 covers cellctl's
decisions; this file covers what only the real protocol shows: ETags,
versions, signed links that expire, and encryption at rest.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from cellctl.exports import ExportRow, ExportStorage, etag_digest
from cellctl.reconcile import ClusterConfig, SecretsConfig, _staging_s3_client

ENDPOINT = os.environ.get("CELLCTL_TEST_S3_ENDPOINT")
pytestmark = pytest.mark.skipif(not ENDPOINT, reason="set CELLCTL_TEST_S3_ENDPOINT to a disposable S3 store")
CELL = "aaaaaaaaaaaaaaaa"
PART_BYTES = 5 * 1024 * 1024  # S3's smallest part that is not the last


@pytest.fixture
def storage():
    # The client cellctl itself builds for its staging key.
    client = _staging_s3_client(
        ClusterConfig(object_storage_bucket="unused", cell_data_endpoint=ENDPOINT),
        SecretsConfig(staging_key_id=os.environ["CELLCTL_TEST_S3_ACCESS_KEY"],
                      staging_application_key=os.environ["CELLCTL_TEST_S3_SECRET_KEY"]),
    )
    bucket = f"export-test-{uuid.uuid4().hex[:12]}"
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    try:
        yield ExportStorage(client, bucket)
    finally:
        for page in client.get_paginator("list_object_versions").paginate(Bucket=bucket):
            for version in page.get("Versions", []) + page.get("DeleteMarkers", []):
                client.delete_object(Bucket=bucket, Key=version["Key"], VersionId=version["VersionId"])
        for page in client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
            for upload in page.get("Uploads", []):
                client.abort_multipart_upload(Bucket=bucket, Key=upload["Key"], UploadId=upload["UploadId"])
        client.delete_bucket(Bucket=bucket)


def _put(url: str, data: bytes) -> str:
    with urlopen(Request(url, data=data, method="PUT"), timeout=30) as response:
        return response.headers["ETag"]


def _upload(storage: ExportStorage, archive: bytes) -> tuple[ExportRow, list[str], dict]:
    """What cellctl and the export Job do before completion: open the upload,
    presign parts, PUT the archive through them, and report it."""
    item = ExportRow(str(uuid.uuid4()), CELL, "requested", datetime.now(UTC), ready_at=datetime.now(UTC))
    item = replace(item, upload_id=storage.create(item))
    urls = storage.part_urls(item, 3, expires_in=600)
    chunks = [archive[offset:offset + PART_BYTES] for offset in range(0, len(archive), PART_BYTES)]
    etags = [_put(url, chunk) for url, chunk in zip(urls, chunks, strict=False)]
    return item, urls, {"bytes": len(archive), "sha256": "0" * 64, "parts": len(etags), "etag_digest": etag_digest(etags)}


def _versions(storage: ExportStorage, key: str) -> list[dict]:
    page = storage.client.list_object_versions(Bucket=storage.bucket, Prefix=key)
    return [entry for entry in page.get("Versions", []) + page.get("DeleteMarkers", []) if entry["Key"] == key]


def test_a_verified_export_is_encrypted_and_its_link_serves_only_that_version_until_it_expires(storage):
    """Defect: a link serves a later overwrite, outlives its five minutes, or the object is stored unencrypted."""
    archive = os.urandom(PART_BYTES + 1024)
    item, _urls, report = _upload(storage, archive)
    version = storage.complete(item, report)
    item = replace(item, archive_version_id=version)
    head = storage.client.head_object(Bucket=storage.bucket, Key=item.key, VersionId=version)
    assert head["ServerSideEncryption"] == "AES256"
    storage.client.put_object(Bucket=storage.bucket, Key=item.key, Body=b"a later overwrite")

    with urlopen(storage.link(item, expires_in=300), timeout=30) as response:
        assert response.read() == archive
        assert response.headers["Content-Disposition"].startswith("attachment;")
    short = storage.link(item, expires_in=1)
    time.sleep(2)
    with pytest.raises(HTTPError) as refused:
        urlopen(short, timeout=30)
    assert refused.value.code == 403


def test_a_substituted_part_is_refused_and_the_upload_is_removed(storage):
    """Defect: someone holding a part URL replaces part of the owner's archive before completion."""
    archive = os.urandom(PART_BYTES + 1024)
    item, urls, report = _upload(storage, archive)
    _put(urls[0], os.urandom(PART_BYTES))  # same size, other bytes
    with pytest.raises(ValueError, match="digest"):
        storage.complete(item, report)
    storage.remove(item)
    assert storage.client.list_multipart_uploads(Bucket=storage.bucket).get("Uploads", []) == []
    assert _versions(storage, item.key) == []


def test_removal_deletes_every_version_of_the_export_and_nothing_beside_it(storage):
    """Defect: an expired export survives as an older version or a delete marker, or removal reaches another object."""
    item, _urls, report = _upload(storage, os.urandom(1024))
    storage.complete(item, report)
    storage.client.put_object(Bucket=storage.bucket, Key=item.key, Body=b"second version")
    storage.client.delete_object(Bucket=storage.bucket, Key=item.key)  # a delete marker on top
    neighbour = f"cells/{CELL}/exports/{uuid.uuid4()}.zip"
    storage.client.put_object(Bucket=storage.bucket, Key=neighbour, Body=b"another export")
    assert len(_versions(storage, item.key)) == 3
    storage.remove(item)
    assert _versions(storage, item.key) == []
    assert len(_versions(storage, neighbour)) == 1
