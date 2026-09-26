"""HTTP clients that resolve the rehearsal's public names to local ports.

The rehearsal has no public DNS. Instead of editing /etc/hosts, every client
connects `(hostname, 443)` to the published local port for that name, while
TLS still verifies the certificate for the real hostname (SNI and Host are
untouched) against the run's own CA and nothing else.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpcore
import httpx


class _MappedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, routes: Mapping[tuple[str, int], tuple[str, int]]) -> None:
        self._routes = dict(routes)
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:  # noqa: E501
        host, port = self._routes.get((host, port), (host, port))
        return await self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.AsyncNetworkStream:  # noqa: E501
        return await self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _SyncMappedBackend(httpcore.NetworkBackend):
    def __init__(self, routes: Mapping[tuple[str, int], tuple[str, int]]) -> None:
        self._routes = dict(routes)
        self._inner = httpcore.SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.NetworkStream:  # noqa: E501
        host, port = self._routes.get((host, port), (host, port))
        return self._inner.connect_tcp(host, port, timeout=timeout, local_address=local_address, socket_options=socket_options)

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.NetworkStream:  # noqa: E501
        return self._inner.connect_unix_socket(path, timeout=timeout, socket_options=socket_options)

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class Resolver:
    """Hostname -> local port for every public name in the run."""

    def __init__(self, ca_path: Path, ports: Mapping[str, int]) -> None:
        self.ca_path = ca_path
        self.routes = {(host, 443): ("127.0.0.1", port) for host, port in ports.items()}

    def _ssl_context(self) -> ssl.SSLContext:
        # With cafile set, only that CA is loaded, never the system store.
        context = ssl.create_default_context(cafile=str(self.ca_path))
        return context

    def async_transport(self) -> httpx.AsyncHTTPTransport:
        transport = httpx.AsyncHTTPTransport(verify=self._ssl_context())
        transport._pool = httpcore.AsyncConnectionPool(  # noqa: SLF001 - httpx exposes no backend hook
            ssl_context=self._ssl_context(), network_backend=_MappedBackend(self.routes)
        )
        return transport

    def sync_transport(self) -> httpx.HTTPTransport:
        transport = httpx.HTTPTransport(verify=self._ssl_context())
        transport._pool = httpcore.ConnectionPool(  # noqa: SLF001 - httpx exposes no backend hook
            ssl_context=self._ssl_context(), network_backend=_SyncMappedBackend(self.routes)
        )
        return transport

    def client(self, **kwargs: Any) -> httpx.Client:
        # trust_env=False: an ambient HTTPS_PROXY must never carry rehearsal traffic.
        return httpx.Client(transport=self.sync_transport(), trust_env=False, timeout=kwargs.pop("timeout", 30.0), **kwargs)

    def async_client(self, **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.async_transport(), trust_env=False, timeout=kwargs.pop("timeout", 30.0), **kwargs)
