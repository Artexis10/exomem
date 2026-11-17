"""The one rule for the files that store mode owns (OpenSpec make-vault-store-canonical D1, task 1.2).

Migration, rollback, the reconcile sweep and the projector all judge a path with ``classify`` and
walk only through ``iter_owned``, so none of them takes another's files for created or deleted ones.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .. import reserved_paths, vault
from ..collection_store import ownership as collection_ownership
from ..find_corpus import EntryKind, entry_kind, is_sync_conflict_copy
from ..kbdir import (
    Layout,
    Managed,
    bound,
    is_managed_root_name,
    managed_root,
    managed_root_rel,
    managed_split,
)


class Ownership(StrEnum):
    #: A text file whose exact bytes the store holds and projects: Markdown, the schema overlay, compiled history.
    PAGE = "page"
    #: A file whose bytes stay on disk; the store records its path, hash and size.
    BINARY = "binary"
    #: Not the store's: a read-only input, or the file of another owner.
    NONE = "none"


class SkipReason(StrEnum):
    """Why ``iter_owned`` left out a file that ``classify`` would own: the walk's own closed set of outcomes."""

    SYMLINK = "symlink"
    JUNCTION = "junction"
    NOT_REGULAR = "not-regular"
    HARD_LINKED = "hard-linked"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class OwnedFile:
    rel: str
    ownership: Ownership
    stat: os.stat_result


@dataclass(frozen=True, slots=True)
class Skipped:
    """One owned-looking path that ``iter_owned`` left out; an unreadable directory also names its own path."""

    rel: str
    reason: SkipReason


# The registry's own attribute names the families, so a new family joins by setting it, not by a list here.
_STORE_FAMILIES = frozenset(
    descriptor.id for descriptor in reserved_paths.internal_state_registry() if descriptor.vault_store
)


def _is_dot_name(name: str) -> bool:
    return name.startswith(".")


def _of(managed: Managed | None) -> Managed:
    """The layout that a call judges by: the layout given, else the managed root that the process is bound to."""
    return bound() if managed is None else managed


def _is_root(managed: Managed | None) -> bool:
    """Whether `managed` describes a `root` vault. None reads the bound managed root, which is `nested` until a vault is bound."""
    return _of(managed).layout is Layout.ROOT


def _split(rel: str, managed: Managed | None) -> str | None:
    """The path below the managed root, or None outside it: the wrapper folder in `nested`, the vault root in `root`."""
    chosen = _of(managed)
    return managed_split(rel, layout=chosen.layout, unmanaged=chosen.unmanaged)


def _claimed(rel: str, managed: Managed | None = None) -> Ownership | None:
    """Rules 4 and 5 of ``classify``: the owner that the layers and the reserved registry name, or None."""
    if _is_root(managed):
        # imported here: `layout` imports modules that import this module
        from ..layout import in_collection_layer

        layered = in_collection_layer(rel, folder=True)
    else:
        layered = collection_ownership.relevant(rel)
    if layered:
        return Ownership.NONE
    reserved = reserved_paths.classify_logical(rel)
    if reserved.blocked:
        return Ownership.PAGE if reserved.descriptor_id in _STORE_FAMILIES else Ownership.NONE
    return None


def is_markdown(rel: str) -> bool:
    """Whether a vault-relative path names a Markdown file, in any letter case."""
    # A file extension is a closed set that the format fixes; it names the format, not the meaning.
    return rel.lower().endswith(".md")


def classify(rel: str, managed: Managed | None = None) -> Ownership:
    """Who owns one vault-relative POSIX path. It does no I/O, and the first matching rule wins.

    1. A path outside the managed root is a read-only input: NONE.
    2. A path with a dot-named component below the managed root is NONE.
    3. A Syncthing conflict copy is NONE, as the managed root walk treats it.
    4. A Records or Planning layer belongs to the collection store: NONE.
    5. A reserved family belongs to its own owner: NONE, unless its descriptor sets ``vault_store``.
    6. The schema overlay is PAGE, whatever its extension.
    7. Any other ``.md`` file, in any letter case, is PAGE.
    8. Everything else is BINARY.

    The managed root is the wrapper folder in a `nested` vault. A call without `managed` reads
    the managed root that the process is bound to, which is `nested` until a vault is bound. In a `root` vault it is the vault root, less the import staging folder and
    the entries that the layout record lists as unmanaged (`layout.managed_of` returns both).
    The store owns content that search hides: ``_trash``, ``_archive``, ``_attachments`` and ``_Adoption``
    are owned, although the managed root walk skips them.
    """
    below = _split(rel, managed)
    if below is None:
        return Ownership.NONE
    parts = below.split("/")
    if any(_is_dot_name(part) for part in parts) or is_sync_conflict_copy(parts[-1]):
        return Ownership.NONE
    claimed = _claimed(rel, managed)
    if claimed is not None:
        return claimed
    if len(parts) > 1 and parts[0] == vault.SCHEMA_OVERLAY_DIRNAME:
        return Ownership.PAGE
    if is_markdown(rel):
        return Ownership.PAGE
    return Ownership.BINARY


def classify_many(rels: Iterable[str], managed: Managed | None = None) -> dict[str, Ownership]:
    """``classify`` for each distinct path of ``rels``, by the same rule: a path costs one classification."""
    return {rel: classify(rel, managed) for rel in dict.fromkeys(rels)}


def owns_folder(rel: str, managed: Managed | None = None) -> bool:
    """Whether the store owns the files that a folder holds, by the rules ``iter_owned`` walks by.

    A folder below the managed root (or that root itself) that no dot-named component,
    collection layer or reserved family takes. It does no I/O.
    """
    if _is_root(managed):
        below = "" if rel in ("", ".") else _split(rel, managed)
    else:
        below = "" if is_managed_root_name(rel) else _split(rel, managed)
    if below is None:
        return False
    if any(_is_dot_name(part) for part in below.split("/")):
        return False
    if below == "" and _is_root(managed):
        return True
    return _claimed(rel, managed) is not Ownership.NONE


def iter_owned(
    vault_root: Path,
    *,
    on_skip: Callable[[Skipped], None] | None = None,
    managed: Managed | None = None,
) -> Iterator[OwnedFile]:
    """Yield every regular file that the store owns below the managed root.

    The walk is depth first and takes each folder's entries by name. It judges an entry as the Knowledge
    Base walk does (``find_corpus.entry_kind``): it leaves out a symlink, a junction, a file that is not
    regular and a file with another hard link, and it never descends a dot-named folder.
    It does not enter a folder that the collection layers or a reserved family own.
    It hands ``on_skip`` each owned-looking path it left out, and each folder it could not read,
    so a caller can tell a partial walk from a whole one. Without ``on_skip`` the walk stays silent.
    ``managed`` is the vault's layout and unmanaged entries, as for ``classify``; the walk and the rule
    must read the same layout, so the walk reads it once, here, and without ``managed`` it reads the
    bound managed root without looking the layout up.
    """
    report = on_skip or (lambda _skipped: None)
    chosen = _of(managed)
    in_root = chosen.layout is Layout.ROOT
    yield from _walk(
        managed_root(Path(vault_root), layout=chosen.layout),
        "" if in_root else managed_root_rel(layout=chosen.layout),
        report,
        chosen,
    )


# Both enums are closed sets that code implements. A directory and a regular file never reach this map.
_SKIP_FOR_KIND = {
    EntryKind.SYMLINK: SkipReason.SYMLINK,
    EntryKind.JUNCTION: SkipReason.JUNCTION,
    EntryKind.HARD_LINKED: SkipReason.HARD_LINKED,
    EntryKind.OTHER: SkipReason.NOT_REGULAR,
}


def _walk(
    directory: Path, rel_dir: str, report: Callable[[Skipped], None], managed: Managed | None
) -> Iterator[OwnedFile]:
    try:
        with os.scandir(directory) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
    except OSError:
        report(Skipped(rel_dir, SkipReason.UNREADABLE))
        return
    for entry in entries:
        if _is_dot_name(entry.name):
            continue
        rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
        try:
            # The directory entry's own stat has no link count on Windows, so the entry is stat'ed here.
            info = os.lstat(entry.path)
        except OSError:
            if classify(rel, managed) is not Ownership.NONE:
                report(Skipped(rel, SkipReason.UNREADABLE))
            continue
        kind = entry_kind(entry.path, info)
        if kind is EntryKind.DIRECTORY:
            if _split(rel, managed) is not None and _claimed(rel, managed) is not Ownership.NONE:
                yield from _walk(Path(entry.path), rel, report, managed)
            continue
        owner = classify(rel, managed)
        if owner is Ownership.NONE:
            continue
        if kind is EntryKind.REGULAR:
            yield OwnedFile(rel, owner, info)
        else:
            report(Skipped(rel, _SKIP_FOR_KIND[kind]))
