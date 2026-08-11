"""Closed provider registry: an unknown configuration cannot silently substitute a row."""

from __future__ import annotations

from collections.abc import Callable

from protocol.namespace import derive_namespace

from .base import ProviderRuntimeBinding, ProviderSpec

from .exomem_direct import ExomemDirectProvider
from .hybrid_rag_direct import HybridRagDirectProvider
from .null_direct import NullDirectProvider

def _state_and_root(context, provider):
    state = getattr(provider, "export_state")()
    ids = sorted(str(getattr(item, "chunk_id", getattr(item, "source_id", item))) for item in state)
    root = context.work_root
    if not root.exists():
        path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []}
    elif root.is_dir():
        path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "directory", "entries": sorted(item.name for item in root.iterdir())}
    else:
        path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "regular", "entries": []}
    return (
        {"kind": "namespace-membership", "expected_namespace": context.namespace, "live_namespaces": []},
        {"kind": "provider-state", "remaining_record_ids": ids, "backend_active": False},
        path,
    )


_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _state_and_root)

_REGISTRY: dict[str, ProviderSpec] = {
    "exomem-source-only": ProviderSpec(ExomemDirectProvider, "exomem-source-only", "exomem", lambda run_id, session_id: derive_namespace(run_id, session_id, "exomem"), _BINDING),
    "hybrid-rag-control": ProviderSpec(HybridRagDirectProvider, "hybrid-rag-control", "hybrid-rag", lambda run_id, session_id: derive_namespace(run_id, session_id, "hybrid-rag"), _BINDING),
    "no-memory": ProviderSpec(NullDirectProvider, "no-memory", "null", lambda run_id, session_id: derive_namespace(run_id, session_id, "null"), _BINDING),
}


def provider_factory(name: str) -> Callable[[], object]:
    return provider_spec(name).factory


def provider_spec(name: str) -> ProviderSpec:
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown direct provider {name!r}") from exc


def registered_provider_names() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
