"""Shared network guard for artifact-only benchmark report regeneration."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def offline_guard() -> Iterator[None]:
    original = socket.socket.connect

    def refused(self, address):  # type: ignore[no-untyped-def]
        del self, address
        raise OSError("offline report generation forbids socket.connect")

    socket.socket.connect = refused
    try:
        yield
    finally:
        socket.socket.connect = original


__all__ = ["offline_guard"]
