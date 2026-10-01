"""Explicit per-vault preview binding for built-in facade contract tests.

The environment flag alone never changes file-mode routing. A trusted caller
must bind the already lease-owned connection; GA mode resolution is a later slice.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from .. import structured_collections as collections
from .connection import CollectionStoreError, WriterConnection
from .writer import CollectionWriter

_BOUND: ContextVar[tuple[Path, CollectionWriter] | None] = ContextVar(
    "collection_store_preview", default=None
)


@contextmanager
def preview_store(vault_root: Path, handle: WriterConnection) -> Iterator[CollectionWriter]:
    """Bind a dark writer for this call context; ownership remains with the caller."""
    root = Path(vault_root).resolve()
    writer = CollectionWriter(root, handle)
    token = _BOUND.set((root, writer))
    try:
        yield writer
    finally:
        _BOUND.reset(token)


def bound_writer(vault_root: Path) -> CollectionWriter | None:
    binding = _BOUND.get()
    return binding[1] if binding is not None and binding[0] == Path(vault_root).resolve() else None


def dispatch(
    vault_root: Path, profile: str, action: str, values: Mapping[str, Any]
) -> tuple[bool, Any]:
    """Use a bound preview only after the facade's unchanged argument validation."""
    binding = _BOUND.get()
    if binding is None or binding[0] != Path(vault_root).resolve():
        return False, None
    if os.environ.get("EXOMEM_COLLECTION_STORE_PREVIEW") != "1":
        raise CollectionStoreError(
            "COLLECTION_STORE_PREVIEW_REQUIRED", "collection store writers are dark"
        )
    writer = binding[1]
    args = {name: value for name, value in values.items() if value is not None}
    if action == "describe":
        return False, None
    writer._facade_profile = profile
    # Records inspection is generic in file mode; only its mutations require
    # the Records profile. Planning inspection retains its profile guard.
    if action == "inspect" and profile == "records":
        return True, writer.inspect_collection(args["collection"], facade_profile=profile)
    if action == "inspect":
        return True, writer.inspect_collection(args["collection"])
    if action == "create":
        return True, writer.create_collection(**args)
    collection = args.pop("collection")
    if profile == "planning" and "plan_id" in args:
        args["item_key"] = args.pop("plan_id")
    if action in {"append", "add"}:
        return True, writer.append_record(collection, **args)
    if action == "triage":
        args["changes"] = args.pop("transition")
        args["operation"] = "triage"
    if action in {"update", "triage"}:
        args.setdefault("changes", {})
        args["refresh_presentation"] = args.get("refresh_presentation") is True
        return True, writer.update_record(collection, **args)
    if action == "revise":
        return True, writer.revise_collection(collection, **args)
    if action == "discard":
        return True, writer.discard_held(collection, **args)
    raise collections.CollectionError(
        "COLLECTION_STORE_PREVIEW_UNSUPPORTED", "this operation belongs to a later store slice"
    )
