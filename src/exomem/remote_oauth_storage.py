"""Shared OAuth and session state for active/passive Exomem replicas.

This adapter carries already-encrypted values to an always-on HTTP coordinator.
Legacy FastMCP OAuth bookkeeping may use its bounded cache; durable Exomem
session collections bypass that cache and remain remote-canonical.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any, SupportsFloat

import httpx

_COORDINATOR_USER_AGENT = (
    "Mozilla/5.0 (compatible; Exomem-Coordinator/1.0; +https://github.com/Artexis10/exomem)"
)


class RemoteOAuthStorage:
    """Implement ``key_value.aio.AsyncKeyValue`` over Exomem's state API."""

    _CACHEABLE_COLLECTIONS = {
        "mcp-jti-mappings",
        "mcp-upstream-tokens",
        "mcp-oauth-proxy-clients",
    }

    def __init__(
        self,
        *,
        url: str,
        namespace: str,
        token: str | None = None,
        timeout: float = 5.0,
        cache_ttl: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.namespace = namespace
        self.token = token
        self.timeout = timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self.transport = transport
        self._cache: dict[tuple[str | None, str], tuple[float, dict[str, Any]]] = {}

    def _cached(self, collection: str | None, key: str) -> dict[str, Any] | None:
        if collection not in self._CACHEABLE_COLLECTIONS:
            return None
        hit = self._cache.get((collection, key))
        if hit is None:
            return None
        if hit[0] <= time.monotonic():
            self._cache.pop((collection, key), None)
            return None
        return hit[1]

    def _remember(
        self,
        collection: str | None,
        key: str,
        value: Mapping[str, Any] | None,
        ttl: float | None = None,
    ) -> None:
        if collection not in self._CACHEABLE_COLLECTIONS:
            return
        if value is None or self.cache_ttl <= 0:
            self._cache.pop((collection, key), None)
            return
        lifetime = self.cache_ttl if ttl is None else min(self.cache_ttl, ttl)
        self._cache[(collection, key)] = (time.monotonic() + max(0.0, lifetime), dict(value))

    async def _request(self, operation: str, payload: dict[str, Any]) -> Any:
        headers = {"Accept": "application/json", "User-Agent": _COORDINATOR_USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(
                f"{self.url}/v1/state/{self.namespace}/{operation}",
                json=payload,
                headers=headers,
            )
        response.raise_for_status()
        return response.json().get("result")

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        cached = self._cached(collection, key)
        if cached is not None:
            return cached
        value = await self._request("get", {"key": key, "collection": collection})
        self._remember(collection, key, value)
        return value

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        result = await self._request("ttl", {"key": key, "collection": collection})
        self._remember(collection, key, result[0], result[1])
        return (result[0], result[1])

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self._request(
            "put",
            {
                "key": key,
                "value": dict(value),
                "collection": collection,
                "ttl": float(ttl) if ttl is not None else None,
            },
        )
        self._remember(collection, key, value, float(ttl) if ttl is not None else None)

    async def put_if_absent(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> bool:
        """Atomically create a value without replacing an existing one."""
        created = bool(
            await self._request(
                "put-if-absent",
                {
                    "key": key,
                    "value": dict(value),
                    "collection": collection,
                    "ttl": float(ttl) if ttl is not None else None,
                },
            )
        )
        if created:
            self._remember(collection, key, value, float(ttl) if ttl is not None else None)
        return created

    async def list_keys(self, *, collection: str | None = None) -> list[str]:
        """Return opaque keys in a coordinator collection."""
        result = await self._request("list-keys", {"collection": collection})
        return [str(key) for key in result]

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        deleted = bool(await self._request("delete", {"key": key, "collection": collection}))
        self._remember(collection, key, None)
        return deleted

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        values = await self._request("get-many", {"keys": list(keys), "collection": collection})
        for key, value in zip(keys, values, strict=True):
            self._remember(collection, key, value)
        return values

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        result = await self._request("ttl-many", {"keys": list(keys), "collection": collection})
        return [(item[0], item[1]) for item in result]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self._request(
            "put-many",
            {
                "keys": list(keys),
                "values": [dict(value) for value in values],
                "collection": collection,
                "ttl": float(ttl) if ttl is not None else None,
            },
        )
        item_ttl = float(ttl) if ttl is not None else None
        for key, value in zip(keys, values, strict=True):
            self._remember(collection, key, value, item_ttl)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        deleted = int(
            await self._request("delete-many", {"keys": list(keys), "collection": collection})
        )
        for key in keys:
            self._remember(collection, key, None)
        return deleted


class ReadThroughMirrorStorage:
    """Use remote state as canonical while lazily migrating a local FastMCP store."""

    def __init__(self, *, primary: Any, fallback: Any):
        self.primary = primary
        self.fallback = fallback

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        value = await self.primary.get(key, collection=collection)
        if value is not None:
            return value
        value, ttl = await self.fallback.ttl(key, collection=collection)
        if value is not None:
            await self.primary.put(key, value, collection=collection, ttl=ttl)
        return value

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        value, ttl = await self.primary.ttl(key, collection=collection)
        if value is not None:
            return value, ttl
        value, ttl = await self.fallback.ttl(key, collection=collection)
        if value is not None:
            await self.primary.put(key, value, collection=collection, ttl=ttl)
        return value, ttl

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self.primary.put(key, value, collection=collection, ttl=ttl)
        await self.fallback.put(key, value, collection=collection, ttl=ttl)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        primary = await self.primary.delete(key, collection=collection)
        fallback = await self.fallback.delete(key, collection=collection)
        return primary or fallback

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        return [await self.get(key, collection=collection) for key in keys]

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        return [await self.ttl(key, collection=collection) for key in keys]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self.primary.put_many(keys, values, collection=collection, ttl=ttl)
        await self.fallback.put_many(keys, values, collection=collection, ttl=ttl)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        primary = await self.primary.delete_many(keys, collection=collection)
        fallback = await self.fallback.delete_many(keys, collection=collection)
        return max(primary, fallback)
