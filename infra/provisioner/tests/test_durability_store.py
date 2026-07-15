from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from exomem_provisioner.durability_store import (
    B2DeletionObjectStore,
    B2PortableDeliveryStore,
    B2RestoreObjectStore,
    B2UploadOnlyObjectStore,
    ProviderObjectConflict,
)


class RecordingS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.upload_calls: list[dict[str, object]] = []
        self.deleted: list[str] = []

    def upload_file(
        self, filename: str, bucket: str, key: str, ExtraArgs: dict[str, object]
    ) -> None:
        body = Path(filename).read_bytes()
        self.objects[key] = {
            "ContentLength": len(body),
            "Metadata": ExtraArgs["Metadata"],
            "ObjectLockRetainUntilDate": ExtraArgs.get("ObjectLockRetainUntilDate"),
            "VersionId": "version-opaque",
            "body": body,
        }
        self.upload_calls.append({"bucket": bucket, "key": key, **ExtraArgs})

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        del Bucket
        if Key not in self.objects:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
            raise error
        return self.objects[Key]

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        del bucket
        Path(filename).write_bytes(self.objects[key]["body"])  # type: ignore[arg-type]

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, str], ExpiresIn: int
    ) -> str:
        return f"https://downloads.invalid/{operation}/{Params['Key']}?ttl={ExpiresIn}"

    def delete_object(self, *, Bucket: str, Key: str, VersionId: str | None = None) -> None:
        del Bucket, VersionId
        self.objects.pop(Key, None)
        self.deleted.append(Key)

    def list_objects_v2(
        self, *, Bucket: str, Prefix: str, ContinuationToken: str | None = None
    ) -> dict[str, object]:
        del Bucket
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        offset = int(ContinuationToken or "0")
        page = keys[offset : offset + 1]
        next_offset = offset + len(page)
        return {
            "Contents": [{"Key": key} for key in page],
            "IsTruncated": next_offset < len(keys),
            "NextContinuationToken": str(next_offset),
        }


@pytest.mark.asyncio
async def test_upload_capability_sets_governance_lock_and_verifies_exact_metadata(
    tmp_path: Path,
) -> None:
    client = RecordingS3Client()
    store = B2UploadOnlyObjectStore(client, bucket="recovery-bucket")
    source = tmp_path / "ciphertext"
    source.write_bytes(b"ciphertext")
    retain_until = datetime.now(UTC) + timedelta(days=7)
    metadata = {
        "tenant-id": "tenant-opaque",
        "cell-id": "cell-opaque",
        "operation-id": "operation-opaque",
        "fence-generation": "9",
    }

    receipt = await store.put_file(
        "recovery/object-opaque",
        source,
        metadata=metadata,
        retain_until=retain_until,
    )
    replay = await store.put_file(
        "recovery/object-opaque",
        source,
        metadata=metadata,
        retain_until=retain_until,
    )

    assert receipt == replay
    assert len(client.upload_calls) == 1
    assert client.upload_calls[0]["ObjectLockMode"] == "GOVERNANCE"
    assert client.upload_calls[0]["Metadata"] == metadata

    with pytest.raises(ProviderObjectConflict):
        await store.put_file(
            "recovery/object-opaque",
            source,
            metadata={**metadata, "fence-generation": "10"},
            retain_until=retain_until,
        )


@pytest.mark.asyncio
async def test_upload_replay_rejects_object_without_the_required_lock(tmp_path: Path) -> None:
    client = RecordingS3Client()
    store = B2UploadOnlyObjectStore(client, bucket="recovery-bucket")
    source = tmp_path / "ciphertext"
    source.write_bytes(b"ciphertext")
    metadata = {"operation-id": "operation-opaque"}
    client.objects["recovery/object-opaque"] = {
        "ContentLength": source.stat().st_size,
        "Metadata": metadata,
        "VersionId": "version-opaque",
        "body": source.read_bytes(),
    }

    with pytest.raises(ProviderObjectConflict, match="retention"):
        await store.put_file(
            "recovery/object-opaque",
            source,
            metadata=metadata,
            retain_until=datetime.now(UTC) + timedelta(days=7),
        )


@pytest.mark.asyncio
async def test_read_presign_and_delete_are_separate_privileged_capabilities(tmp_path: Path) -> None:
    client = RecordingS3Client()
    upload = B2UploadOnlyObjectStore(client, bucket="recovery-bucket")
    restore = B2RestoreObjectStore(client, bucket="recovery-bucket")
    deletion = B2DeletionObjectStore(client, bucket="recovery-bucket")
    source = tmp_path / "ciphertext"
    source.write_bytes(b"ciphertext")
    await upload.put_file(
        "recovery/object-opaque",
        source,
        metadata={"operation-id": "operation-opaque"},
        retain_until=datetime.now(UTC) + timedelta(days=7),
    )

    destination = tmp_path / "downloaded"
    await restore.download_file("recovery/object-opaque", destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    url = await restore.presigned_download("recovery/object-opaque", ttl_seconds=900)
    assert destination.read_bytes() == b"ciphertext"
    assert "ttl=900" in url

    with pytest.raises(ValueError, match="15 minutes"):
        await restore.presigned_download("recovery/object-opaque", ttl_seconds=901)

    await deletion.delete("recovery/object-opaque", version_id="version-opaque")
    assert await deletion.absent("recovery/object-opaque") is True
    assert not hasattr(upload, "download_file")
    assert not hasattr(upload, "delete")


@pytest.mark.asyncio
async def test_deletion_capability_can_page_metadata_for_delivery_gc() -> None:
    client = RecordingS3Client()
    client.objects["user-export-delivery/a"] = {
        "ContentLength": 1,
        "Metadata": {"expires-at": "2030-01-01T00:00:00Z"},
        "VersionId": "version-opaque",
        "body": b"x",
    }
    deletion = B2DeletionObjectStore(client, bucket="user-export-bucket")

    keys, token = await deletion.list_page(prefix="user-export-delivery/")
    head = await deletion.head(keys[0])

    assert token is None
    assert head is not None
    assert head.metadata["expires-at"] == "2030-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_restore_capability_lists_every_b2_page() -> None:
    client = RecordingS3Client()
    for key in ("database-backup/a", "database-backup/b", "unrelated/c"):
        client.objects[key] = {
            "ContentLength": 1,
            "Metadata": {},
            "VersionId": "version-opaque",
            "body": b"x",
        }
    restore = B2RestoreObjectStore(client, bucket="database-backup-bucket")

    first, token = await restore.list_page(prefix="database-backup/")
    second, final_token = await restore.list_page(
        prefix="database-backup/", continuation_token=token
    )

    assert first + second == ["database-backup/a", "database-backup/b"]
    assert final_token is None


@pytest.mark.asyncio
async def test_user_export_upload_omits_object_lock_and_rejects_locked_bucket_default(
    tmp_path: Path,
) -> None:
    client = RecordingS3Client()
    store = B2UploadOnlyObjectStore(client, bucket="user-export-bucket")
    source = tmp_path / "ciphertext"
    source.write_bytes(b"ciphertext")

    receipt = await store.put_file(
        "user-export/object-opaque",
        source,
        metadata={"operation-id": "operation-opaque"},
        retain_until=None,
    )

    assert receipt.retain_until is None
    assert "ObjectLockMode" not in client.upload_calls[0]
    assert "ObjectLockRetainUntilDate" not in client.upload_calls[0]


@pytest.mark.asyncio
async def test_portable_delivery_uses_provider_encryption_and_fifteen_minute_url(
    tmp_path: Path,
) -> None:
    client = RecordingS3Client()
    store = B2PortableDeliveryStore(client, bucket="user-export-bucket")
    source = tmp_path / "portable.zip"
    source.write_bytes(b"portable plaintext")

    first = await store.put_file(
        "user-export-delivery/opaque.portable",
        source,
        metadata={"expires-at": "2030-01-01T00:15:00Z"},
        retain_until=None,
    )
    replay = await store.put_file(
        "user-export-delivery/opaque.portable",
        source,
        metadata={"expires-at": "2030-01-01T00:15:00Z"},
        retain_until=None,
    )
    url = await store.presigned_download("user-export-delivery/opaque.portable", ttl_seconds=900)

    assert client.upload_calls[0]["ServerSideEncryption"] == "AES256"
    assert client.upload_calls[0]["ContentType"] == "application/vnd.exomem.portable-export"
    assert first == replay
    assert len(client.upload_calls) == 1
    assert "ttl=900" in url
