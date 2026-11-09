"""Where a store-mode writer looks for its page, and what it refuses (store task 2.2).

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
    """``resolve_under_vault`` for an existing file, with the store's answer for a page.

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


def holds_pages_under(vault_root: Path, folder: str) -> bool:
    """Whether the store holds a page below ``folder``, whatever the disk shows of it."""
    with read.snapshot(vault_root) as view:
        return next(view.pages(f"{folder}/"), None) is not None


def inbound_links(vault_root: Path, rel: str) -> list[vault.InboundLink]:
    """The links to the stored page ``rel``, as ``find_inbound_wikilinks`` gives, from the store.

    The disk form walks the vault's files, and a page that a commit stored has none until the
    projector writes it, so it cannot see a link that the last commit added. The graph rows are
    derived in each commit, so they name the linking pages as of the current revision; each linking
    page's stored bytes then give the line. A link that the graph resolved by a form the line scan
    does not match (a title, an alias) is reported at line 0 with no context, never dropped.
    """
    forms = set(vault._inbound_target_forms(rel)[1:])
    links: list[vault.InboundLink] = []
    with read.snapshot(vault_root) as view:
        for source in sorted(view.inbound_sources(rel)):
            page = view.page(source)
            text = "" if page is None else page.body.decode("utf-8", errors="replace")
            found = [
                vault.InboundLink(source, line, context, raw)
                for line, context, raw in vault._scan_wikilinks(text)
                if vault._inbound_bucket_key(raw) in forms
            ]
            links.extend(found or [vault.InboundLink(source, 0, "", "")])
    return links


def refuse_in_store_mode(vault_root: Path, operation: str, why: str) -> None:
    """Refuse ``operation`` while the vault is in store mode, for a writer whose paths are pages.

    It prevents the same disk write as ``refuse_unrouted`` and is the form for a writer that has no
    path to test before it starts, so it costs a rollback to run that writer on files.
    """
    if mode.store_mode_active(vault_root):
        raise availability.unrouted(operation, why)


def owns_any(
    vault_root: Path, *, files: tuple[str, ...] = (), folders: tuple[str, ...] = ()
) -> bool:
    """Whether the vault is in store mode and the store owns any path that it was given.

    A file-mode vault and a path that the store does not own are the disk's. A path that does not
    resolve is no one's here, because the writer's own path check names that error.
    """
    if not mode.store_mode_active(vault_root):
        return False
    try:
        return any(
            ownership.classify(vault.resolve_under_vault(vault_root, path)[1])
            is not ownership.Ownership.NONE
            for path in files
        ) or any(
            ownership.owns_folder(vault.resolve_under_vault(vault_root, path)[1])
            for path in folders
        )
    except vault.VaultPathError:
        return False


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
    file-mode vault and a path that the store does not own pass: the disk writer serves them.
    """
    if owns_any(vault_root, files=files, folders=folders):
        raise availability.unrouted(operation, why)
