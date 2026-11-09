"""Where a store-mode writer looks for its page, and what it refuses (OpenSpec make-vault-store-canonical, task 2.2).

A page that a commit stored has no file until the projector writes it, so a writer that asks the
disk whether its source exists, or whether its destination is free, answers for the projection and
not for the vault. These helpers answer from the store for the paths that the store owns, and from
the disk for the rest. A writer whose store form does not exist yet refuses through the same place.
"""

from __future__ import annotations

from pathlib import Path

from .. import vault
from . import availability, mode, ownership, read


def holds(vault_root: Path, rel: str) -> bool:
    """Whether the vault has a file at ``rel``, as the store sees it.

    A page is held when the store holds it, whatever the disk shows: the projection trails the
    store, so a file left behind by a delete that is still queued is no page. A binary is held when
    the store records it or the disk has it, because binary bytes are written to disk first and the
    importer records them later. A path the store does not own is the disk's.
    """
    kind = ownership.classify(rel)
    if kind is not ownership.Ownership.NONE:
        with read.snapshot(vault_root) as view:
            if kind is ownership.Ownership.PAGE:
                return view.page_meta(rel) is not None
            if view.binary(rel) is not None:
                return True
    return (Path(vault_root) / rel).exists()


def resolve_existing(vault_root: Path, path: str) -> tuple[Path, str]:
    """``resolve_under_vault(must_exist, must_be_file, refuse_withheld)`` with the store's answer for a page.

    It raises the ``VaultPathError`` that the disk form raises: NOT_FOUND for an absent page and
    for a withheld one, whose text is the same, and NOT_A_FILE for a folder.
    """
    resolution = vault.resolve_under_vault(
        vault_root, path, refuse_withheld=True, return_details=True
    )
    rel = resolution.relative
    withheld = vault._write_target_withheld(
        vault_root, resolution.resolved, Path(vault_root).resolve()
    )
    if withheld or not holds(vault_root, rel):
        raise vault.VaultPathError(code="NOT_FOUND", reason=vault.missing_path_reason(rel))
    if resolution.candidate.exists() and not resolution.candidate.is_file():
        raise vault.VaultPathError(code="NOT_A_FILE", reason=f"path is not a regular file: {rel}")
    return resolution.candidate, rel


def refuse_unrouted(
    vault_root: Path,
    operation: str,
    why: str,
    *,
    files: tuple[str, ...] = (),
    folders: tuple[str, ...] = (),
) -> None:
    """Refuse ``operation`` in a store-mode vault when the store owns any path that it was given.

    It runs before the writer reads or changes anything, so a refusal leaves the vault as it was. A
    file-mode vault and a path that the store does not own pass: the disk writer serves them. A path
    that does not resolve passes too, because the writer's own path check names that error.
    """
    if not mode.store_mode_active(vault_root):
        return
    try:
        owned = any(
            ownership.classify(vault.resolve_under_vault(vault_root, path)[1])
            is not ownership.Ownership.NONE
            for path in files
        ) or any(
            ownership.owns_folder(vault.resolve_under_vault(vault_root, path)[1])
            for path in folders
        )
    except vault.VaultPathError:
        return
    if owned:
        raise availability.unrouted(operation, why)
