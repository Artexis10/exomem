"""Closed provider registry: an unknown configuration cannot silently substitute a row."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from protocol.namespace import derive_namespace

from .base import ProviderDescriptor, ProviderRuntimeBinding, ProviderSpec

from .basic_memory_direct import BasicMemoryDirectProvider
from .exomem_direct import ExomemDirectProvider
from .hybrid_rag_direct import HybridRagDirectProvider
from .null_direct import NullDirectProvider

def _root_lstat(context):
    root = context.work_root
    try:
        capability_prefix = f"/proc/{os.getpid()}/fd/"
        mode = (
            os.stat(root).st_mode
            if str(root).startswith(capability_prefix)
            else os.lstat(root).st_mode
        )
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
    vault = context.work_root / "vault"
    retained: list[str] = []
    try:
        mode = os.lstat(vault).st_mode
    except FileNotFoundError:
        active = getattr(adapter, "_mcp", None) is not None
    else:
        active = True
        if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
            for root, directories, files in os.walk(vault, followlinks=False):
                root_path = Path(root)
                retained.extend(
                    (root_path / name).relative_to(vault).as_posix()
                    for name in (*directories, *files)
                )
        else:
            retained.append("vault-root")
    return _rows(context, ids=retained, active=active)


def _hybrid_state(context, provider):
    return _rows(context, ids=(chunk.chunk_id for chunk in provider._chunks), active=provider._index is not None or bool(provider._chunks))


def _null_state(context, provider):
    retained = sorted(str(name) for name in vars(provider))
    return _rows(context, ids=retained, active=bool(retained))


def _basic_memory_state(context, provider):
    """Out-of-process rows owe process absence on top of the usual surfaces."""

    live = provider.live_process_count()
    bound = provider.listener_bound()
    retained = sorted(str(item) for item in provider.transport_log_document_ids())
    # provider-state describes what the provider still holds; the surviving
    # process is the process-group surface's job.  Keeping them separate means
    # a provider that zeroes its own bookkeeping cannot hide a leaked process.
    return _rows(context, ids=retained, active=bool(retained)) + (
        {
            "kind": "process-group",
            "group_ref": "sidecar",
            "remaining_count": live,
            "listener_bound": bound,
        },
    )
_EXOMEM_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _exomem_state)
_HYBRID_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _hybrid_state)
_NULL_BINDING = ProviderRuntimeBinding(("namespace-membership", "provider-state", "session-root"), _null_state)
_BASIC_MEMORY_BINDING = ProviderRuntimeBinding(
    ("namespace-membership", "process-group", "provider-state", "session-root"), _basic_memory_state
)

_FOREGROUND = "in-process-no-post-return-background"
_OWNED_SUBPROCESS = "owned-subprocess-terminated-at-cleanup"

_REGISTRY: dict[str, ProviderSpec] = {
    "exomem-source-only": ProviderSpec(ExomemDirectProvider, ProviderDescriptor("exomem-source-only", _FOREGROUND), "exomem", lambda run_id, session_id: derive_namespace(run_id, session_id, "exomem"), _EXOMEM_BINDING),
    "hybrid-rag-control": ProviderSpec(HybridRagDirectProvider, ProviderDescriptor("hybrid-rag-control", _FOREGROUND), "hybrid-rag", lambda run_id, session_id: derive_namespace(run_id, session_id, "hybrid-rag"), _HYBRID_BINDING),
    "no-memory": ProviderSpec(NullDirectProvider, ProviderDescriptor("no-memory", _FOREGROUND), "null", lambda run_id, session_id: derive_namespace(run_id, session_id, "null"), _NULL_BINDING),
    "basic-memory-direct": ProviderSpec(BasicMemoryDirectProvider, ProviderDescriptor("basic-memory-direct", _OWNED_SUBPROCESS), "basic-memory", lambda run_id, session_id: derive_namespace(run_id, session_id, "basic-memory"), _BASIC_MEMORY_BINDING),
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
