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

from .. import planning
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
    if action == "create":
        proposed = collections.parse_manifest_bytes(
            writer.root, args["manifest_path"], args["manifest_text"].encode()
        )
    else:
        _, proposed, _ = writer._collection(args["collection"])
    if proposed.semantic_profile != profile:
        raise collections.CollectionError(
            "PLANNING_PROFILE_REQUIRED" if profile == "planning" else "RECORDS_PROFILE_REQUIRED",
            f"{profile.title()} collection is required",
        )
    if action == "inspect":
        return True, writer.inspect_collection(args["collection"])
    if action == "create":
        if profile == "planning" and args.get("scaffold", True):
            args["manifest_text"] = planning._with_default_scaffold(args["manifest_text"], proposed)
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
        return True, writer.update_record(collection, **args)
    if action == "revise":
        return True, writer.revise_collection(collection, **args)
    if action == "discard":
        return True, writer.discard_held(collection, **args)
    raise collections.CollectionError(
        "COLLECTION_STORE_PREVIEW_UNSUPPORTED", "this operation belongs to a later store slice"
    )
