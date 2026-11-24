"""The names a Records or Planning write reply says: the user's collection by its title, and the row by its label.

The stored terminal keeps ids, hashes and paths, never titles or values, so a reply reads its names where it
is composed. The collection resolves through the same released-manifest check that a read uses, and the row
label appears only when the caller may read every file the write touched; otherwise the reply names the
collection alone, as it does for a create, revise or rebaseline, which carry no row. A replay resolves
again, so it reads the collection's title and the request's values as they are now.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import mutation_terminal, record_governance, records
from . import structured_collections as collections
from .mutation_terminal import UserNames

log = logging.getLogger(__name__)

# A label is the user's own data and can be long. The sentence keeps the head of it.
_LABEL_CHARS = 80


def resolve(vault_root: Path, result: Any, arguments: Mapping[str, Any]) -> UserNames | None:
    """The names for a committed or replayed Records or Planning row write or restructure, else None.

    None leaves the reply as it is: the write is of another kind, or the caller may not read the collection.
    Naming is optional prose, so a failure here never refuses a write that already committed.
    """
    if not (isinstance(result, Mapping) and result.get("ok") is True and result.get("status") in ("committed", "replayed")):
        return None
    leaf = result.get("leaf_result")
    if not (mutation_terminal.valid_record_receipt(leaf) or mutation_terminal.valid_planning_receipt(leaf)):
        return None
    restructured = isinstance(leaf.get("shape"), Mapping)
    # nosemgrep: ep-word-membership -- the receipt protocol's closed operation names for a write to the collection itself.
    lifecycle = leaf.get("operation") in {"create", "revise", "rebaseline"}
    if not (restructured or lifecycle or isinstance(leaf.get("item_key") or leaf.get("plan_id"), str)):
        return None
    try:
        release = record_governance.full_release_filter(vault_root)
        manifest = collections.resolve_collection(vault_root, leaf["collection_id"], authorize_path=release)
        readable = not restructured and all(release(path) for path in leaf["affected_paths"])
        titles = _titles(vault_root, leaf["shape"]["steps"], release) if restructured else None
        return UserNames(manifest.title, _label(manifest, arguments.get("item")) if readable else None, titles)
    except Exception:  # noqa: BLE001 - names are optional prose and cannot refuse a committed write
        log.debug("reply names did not resolve", exc_info=True)
        return None


def retired_reason(vault_root: Path, error: collections.CollectionError) -> str:
    """The reason of a refused row write, with a merged collection's successor named by title where the caller may read it.

    An error of another kind, or one whose successor does not resolve or is not released, keeps its own reason.
    """
    successor = error.details.get("successor")
    if error.code != "COLLECTION_RETIRED" or not isinstance(successor, str):
        return error.reason
    try:
        release = record_governance.full_release_filter(vault_root)
        title = collections.resolve_collection(vault_root, successor, authorize_path=release).title
    except Exception:  # noqa: BLE001 - names are optional prose: an unresolved successor keeps the plain phrase
        log.debug("a retired collection's successor did not resolve", exc_info=True)
        return error.reason
    return records.retired_message(str(error.details.get("title", "")), title)


def _titles(vault_root: Path, steps: Any, release: Any) -> dict[str, str]:
    """The title of each collection a restructure's steps name as their `target`, for each one the caller may read.

    A target that does not resolve is left out, and the sentence for its step with it.
    """
    titles = {}
    for target in {step.get("target") for step in steps if isinstance(step, Mapping)}:
        if not isinstance(target, str):
            continue
        try:
            titles[target] = collections.resolve_collection(vault_root, target, authorize_path=release).title
        except Exception:  # noqa: BLE001, S112 - names are optional prose: an unresolved target goes unsaid
            log.debug("a restructure target did not resolve", exc_info=True)
    return titles


def _label(manifest: collections.CollectionManifest, item: Any) -> str | None:
    """The row's title field when the manifest declares one, else its natural-key values, from the request's `item`."""
    if not isinstance(item, Mapping):
        return None
    recipe = manifest.item_presentation
    for fields in ((recipe.title.field,) if recipe is not None else (), manifest.schema.natural_key):
        values = [_scalar(item.get(name)) for name in fields]
        if fields and all(values):
            text = " ".join(", ".join(values).split())
            return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"
    return None


def _scalar(value: Any) -> str:
    """A value that reads as words, or an empty string for one that does not."""
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""
