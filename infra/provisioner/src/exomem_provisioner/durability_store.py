"""Capability-separated B2 S3 adapters for recovery objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProviderObjectConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderObjectHead:
    key: str
    size: int
    metadata: dict[str, str]
    version_id: str | None
    retain_until: datetime | None


def _not_found(error: Exception) -> bool:
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    detail = response.get("Error")
    return isinstance(detail, dict) and str(detail.get("Code")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _B2StoreBase:
    def __init__(self, client: Any, *, bucket: str) -> None:
        if not bucket:
            raise ValueError("B2 bucket must be explicit")
        self._client = client
        self._bucket = bucket

    async def head(self, key: str) -> ProviderObjectHead | None:
        try:
            response = await asyncio.to_thread(
                self._client.head_object,
                Bucket=self._bucket,
                Key=key,
            )
        except Exception as error:
            if _not_found(error):
                return None
            raise
        metadata = response.get("Metadata", {})
        if not isinstance(metadata, dict):
            raise ProviderObjectConflict("provider returned invalid object metadata")
        return ProviderObjectHead(
            key=key,
            size=int(response["ContentLength"]),
            metadata={str(name): str(value) for name, value in metadata.items()},
            version_id=(str(response["VersionId"]) if response.get("VersionId") else None),
            retain_until=_as_utc(response.get("ObjectLockRetainUntilDate")),
        )

    async def list_page(
        self, *, prefix: str, continuation_token: str | None = None
    ) -> tuple[list[str], str | None]:
        arguments: dict[str, str] = {"Bucket": self._bucket, "Prefix": prefix}
        if continuation_token is not None:
            arguments["ContinuationToken"] = continuation_token
        response = await asyncio.to_thread(self._client.list_objects_v2, **arguments)
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise ProviderObjectConflict("provider returned invalid object listing")
        keys: list[str] = []
        for item in contents:
            if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
                raise ProviderObjectConflict("provider returned invalid object listing")
            keys.append(item["Key"])
        next_token = response.get("NextContinuationToken") if response.get("IsTruncated") else None
        if next_token is not None and not isinstance(next_token, str):
            raise ProviderObjectConflict("provider returned invalid continuation token")
        return keys, next_token


class B2UploadOnlyObjectStore(_B2StoreBase):
    """Runtime capability: upload and verify only, never read or delete."""

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> ProviderObjectHead:
        existing = await self.head(key)
        expected_size = source.stat().st_size
        normalized_metadata = {name.lower(): value for name, value in metadata.items()}
        if existing is not None:
            if existing.size != expected_size or existing.metadata != normalized_metadata:
                raise ProviderObjectConflict("existing provider object identity differs")
            self._require_retention(existing, retain_until)
            return existing
        extra_args: dict[str, Any] = {
            "Metadata": normalized_metadata,
            "ContentType": "application/vnd.exomem.recovery",
        }
        if retain_until is not None:
            extra_args.update(
                {
                    "ObjectLockMode": "GOVERNANCE",
                    "ObjectLockRetainUntilDate": _as_utc(retain_until),
                }
            )
        await asyncio.to_thread(
            self._client.upload_file,
            str(source),
            self._bucket,
            key,
            ExtraArgs=extra_args,
        )
        receipt = await self.head(key)
        if receipt is None:
            raise ProviderObjectConflict("provider upload lacks independent HEAD proof")
        if receipt.size != expected_size or receipt.metadata != normalized_metadata:
            raise ProviderObjectConflict("provider upload proof differs from expected object")
        self._require_retention(receipt, retain_until)
        return receipt

    @staticmethod
    def _require_retention(receipt: ProviderObjectHead, retain_until: datetime | None) -> None:
        actual = _as_utc(receipt.retain_until)
        expected = _as_utc(retain_until)
        if expected is None:
            if actual is not None:
                raise ProviderObjectConflict("provider object has an unexpected retention lock")
            return
        if actual is None or actual < expected:
            raise ProviderObjectConflict("provider object retention proof is insufficient")


class B2RestoreObjectStore(_B2StoreBase):
    """Short-lived privileged capability: read and presign, never delete."""

    async def download_file(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        temporary = destination.with_name(f".{destination.name}.partial")
        try:
            await asyncio.to_thread(
                self._client.download_file,
                self._bucket,
                key,
                str(temporary),
            )
            temporary.chmod(0o600)
            temporary.replace(destination)
            destination.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    async def presigned_download(self, key: str, *, ttl_seconds: int) -> str:
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("presigned download TTL must be at most 15 minutes")
        return str(
            await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        )


class B2DeletionObjectStore(_B2StoreBase):
    """Short-lived privileged capability: delete and independently prove absence."""

    async def delete(
        self,
        key: str,
        *,
        version_id: str | None = None,
        bypass_governance: bool = False,
    ) -> None:
        arguments: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if version_id is not None:
            arguments["VersionId"] = version_id
        if bypass_governance:
            arguments["BypassGovernanceRetention"] = True
        await asyncio.to_thread(self._client.delete_object, **arguments)

    async def absent(self, key: str) -> bool:
        return await self.head(key) is None


class B2PortableDeliveryStore(_B2StoreBase):
    """Short-lived JIT capability for plaintext portable delivery objects."""

    async def put_file(
        self,
        key: str,
        source: Path,
        *,
        metadata: dict[str, str],
        retain_until: datetime | None,
    ) -> ProviderObjectHead:
        if retain_until is not None:
            raise ProviderObjectConflict("portable delivery must not use Object Lock")
        normalized = {name.lower(): value for name, value in metadata.items()}
        await asyncio.to_thread(
            self._client.upload_file,
            str(source),
            self._bucket,
            key,
            ExtraArgs={
                "Metadata": normalized,
                "ContentType": "application/vnd.exomem.portable-export",
                "ServerSideEncryption": "AES256",
            },
        )
        receipt = await self.head(key)
        if (
            receipt is None
            or receipt.size != source.stat().st_size
            or receipt.metadata != normalized
            or receipt.retain_until is not None
        ):
            raise ProviderObjectConflict("portable delivery upload proof differs")
        return receipt

    async def presigned_download(self, key: str, *, ttl_seconds: int) -> str:
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("presigned download TTL must be at most 15 minutes")
        return str(
            await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=ttl_seconds,
            )
        )
