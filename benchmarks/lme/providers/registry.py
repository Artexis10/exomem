"""Closed provider registry: an unknown configuration cannot silently substitute a row."""

from __future__ import annotations

from collections.abc import Callable

from .exomem_direct import ExomemDirectProvider
from .hybrid_rag_direct import HybridRagDirectProvider
from .null_direct import NullDirectProvider

_REGISTRY: dict[str, Callable[[], object]] = {
    "exomem-source-only": ExomemDirectProvider,
    "hybrid-rag-control": HybridRagDirectProvider,
    "no-memory": NullDirectProvider,
}


def provider_factory(name: str) -> Callable[[], object]:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown direct provider {name!r}") from exc


def registered_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
