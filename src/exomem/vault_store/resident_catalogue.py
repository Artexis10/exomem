"""The resolver over the catalogue's vault pages that the last store commit left, kept for the next one.

It is keyed by vault and stands at one vault triple. The next commit derives from it instead of building
the resolver over every catalogue row again. A commit that starts at another triple (a write that bypassed
the commit, a lexical sync, another process) finds none and builds from the rows. Rebuildable memory, so
every owner of a lifetime that ends it (the store closing, a rollback, the mode leaving store, an idle
process) releases it here.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .. import vault

_LOCK = threading.Lock()
#: Each entry holds a resolver over every page (megabytes at 20,000 pages), so the map keeps the two
#: vaults that a commit used last: one vault is served, and one more covers a vault being switched.
_MAX_VAULTS = 2
#: vault root -> (the vault triple it stands at, the catalogue's ``(path, title)`` rows, the resolver over them)
_HELD: OrderedDict[Path, tuple[tuple, dict[str, str | None], vault.WikilinkResolver]] = OrderedDict()


def remember(
    root: Path,
    triple: tuple,
    catalogue: tuple[dict[str, str | None], vault.WikilinkResolver] | None,
) -> None:
    """Keep ``catalogue`` (the rows and the resolver over them) at ``triple``; None keeps nothing."""
    key = Path(root)
    with _LOCK:
        if catalogue is None:
            _HELD.pop(key, None)
            return
        _HELD[key] = (triple, *catalogue)
        _HELD.move_to_end(key)
        while len(_HELD) > _MAX_VAULTS:
            _HELD.popitem(last=False)


def advance(root: Path, before: tuple, after: tuple) -> None:
    """Keep the rows and resolver that stand at ``before`` at ``after``.

    A commit that writes no catalogue row moves the triple and leaves the rows as they were.
    """
    key = Path(root)
    with _LOCK:
        found = _HELD.get(key)
        if found is not None and found[0] == before:
            _HELD[key] = (after, *found[1:])


def held(root: Path, triple: tuple | None) -> tuple[dict[str, str | None], vault.WikilinkResolver] | None:
    """The rows and resolver when they stand at ``triple``, else None.

    One that stands at another triple is released: the triple only moves forward, so it never serves
    a later commit.
    """
    if triple is None:
        return None
    key = Path(root)
    with _LOCK:
        found = _HELD.get(key)
        if found is None:
            return None
        if found[0] != triple:
            del _HELD[key]
            return None
        _HELD.move_to_end(key)
    return found[1:]


def forget(root: Path | None = None) -> None:
    """Release the entry of the vault at ``root``, or of every vault. A commit builds one again from the rows."""
    with _LOCK:
        if root is None:
            _HELD.clear()
        else:
            _HELD.pop(Path(root), None)
            _HELD.pop(Path(root).resolve(), None)
