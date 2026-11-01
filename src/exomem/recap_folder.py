"""Where conversation recaps live: the shared episodes folder, or a restricted writer's inbox.

One place names the folder, so recording, listing, superseding and recent context agree.
A restricted writer's recaps stay in its private inbox, like its Source captures.
"""

from __future__ import annotations

from pathlib import Path

from . import source_taxonomy
from .kbdir import kb_dirname

#: The shared folder relative to the knowledge base, with its trailing separator.
SHARED_INNER = f"{source_taxonomy.SOURCES_ROOT}/{source_taxonomy.EPISODE_PATH_LABEL}/"
#: A probe leaf: an inbox is judged by the path a recap would take, whatever its name.
_PROBE_LEAF = "recap.md"


def shared_folder() -> str:
    """The vault-relative folder that holds the owner's recaps."""
    return f"{kb_dirname()}/{SHARED_INNER.rstrip('/')}"


def recap_folder(vault_root: Path) -> str:
    """The vault-relative folder in which the current caller records and lists recaps.

    Raises `ValueError` (`WRITE_REFUSED`) for a restricted writer that has no inbox.
    """
    from .governance import connector_boundary

    inbox = connector_boundary.inbox_destination(
        Path(vault_root), source_taxonomy.EPISODE_PATH_LABEL, _PROBE_LEAF,
    )
    return shared_folder() if inbox is None else inbox


def inbox_folders(vault_root: Path | None) -> tuple[str, ...]:
    """The recap folder of each configured capture path, as vault-relative folders."""
    if vault_root is None:
        return ()
    from .governance import connector_boundary

    return tuple(
        f"{folder}/{source_taxonomy.EPISODE_PATH_LABEL}"
        for folder in connector_boundary.capture_folders(Path(vault_root))
    )


def recap_folders(vault_root: Path | None) -> tuple[str, ...]:
    """Every folder that holds recaps: the shared one, then one per capture path."""
    return (shared_folder(), *inbox_folders(vault_root))


def under(folders: tuple[str, ...], rel: str, *, casefold: bool = False) -> bool:
    """Whether `rel` lies under one of `folders`, vault-relative with `/` separators."""
    key = str.casefold if casefold else str
    target = key(rel.replace("\\", "/"))
    return any(target.startswith(key(f"{folder}/")) for folder in folders)


def in_inbox(vault_root: Path | None, rel: str, *, casefold: bool = False) -> bool:
    """Whether `rel` lies anywhere under the recap folder of a configured capture path."""
    return under(inbox_folders(vault_root), rel, casefold=casefold)
