"""Closed provider registry: an unknown configuration cannot silently substitute a row."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable

from protocol.namespace import derive_namespace

from .base import ProviderRuntimeBinding, ProviderSpec

from .exomem_direct import ExomemDirectProvider
from .hybrid_rag_direct import HybridRagDirectProvider
from .null_direct import NullDirectProvider

def _root_lstat(context):
    root = context.work_root
    try:
        mode = os.lstat(root).st_mode
    except FileNotFoundError:
        path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "missing", "entries": []}
    else:
        if stat.S_ISLNK(mode):
            path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "symlink", "entries": []}
        elif stat.S_ISDIR(mode):
            with os.scandir(root) as entries:
                path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "directory", "entries": sorted(item.name for item in entries)}
        elif stat.S_ISREG(mode):
            path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "regular", "entries": []}
        else:
            path = {"kind": "path-lstat", "path": "session-root", "raw_kind": "other", "entries": []}
    return path


def _rows(context, *, ids, active):
    return (
        {"kind": "namespace-membership", "expected_namespace": context.namespace, "live_namespaces": [context.namespace] if active else []},
        {"kind": "provider-state", "remaining_record_ids": sorted(str(item) for item in ids), "backend_active": active},
        _root_lstat(context),
    )


def _exomem_state(context, provider):
    adapter = provider._adapter
    vault = getattr(adapter, "_vault", None)
    source_paths = getattr(adapter, "_source_paths", {})
    return _rows(context, ids=getattr(source_paths, "keys", lambda: ())(), active=vault is not None)


def _hybrid_state(context, provider):
    return _rows(context, ids=(chunk.chunk_id for chunk in provider._chunks), active=provider._index is not None or bool(provider._chunks))


def _null_state(context, provider):
    del provider
    return _rows(context, ids=(), active=False)
_EXOMEM_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _exomem_state)
_HYBRID_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _hybrid_state)
_NULL_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _null_state)

_REGISTRY: dict[str, ProviderSpec] = {
    "exomem-source-only": ProviderSpec(ExomemDirectProvider, "exomem-source-only", "exomem", lambda run_id, session_id: derive_namespace(run_id, session_id, "exomem"), _EXOMEM_BINDING),
    "hybrid-rag-control": ProviderSpec(HybridRagDirectProvider, "hybrid-rag-control", "hybrid-rag", lambda run_id, session_id: derive_namespace(run_id, session_id, "hybrid-rag"), _HYBRID_BINDING),
    "no-memory": ProviderSpec(NullDirectProvider, "no-memory", "null", lambda run_id, session_id: derive_namespace(run_id, session_id, "null"), _NULL_BINDING),
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
