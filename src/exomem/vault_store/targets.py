"""Where a store-mode writer looks for its page, and what it refuses (store task 2.2).

A page that a commit stored has no file until the projector writes it, so a writer that asks the
disk whether its source exists, or whether its destination is free, answers for the projection and
not for the vault. These helpers answer from the store for the paths that the store owns, and from
the disk for the rest. A writer whose store form does not exist yet refuses through the same place.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

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


class InboundCheck(NamedTuple):
    """The links to a stored page, and the linking pages that its delete commit must bind.

    ``unimported`` holds the links from files in the Knowledge Base that the store has no page
    for. No policy covers such a file yet, so the publication filter would hide it from every
    caller. The caller decides whether to count it, and must not name it to a restricted one.
    """

    links: list[vault.InboundLink]
    unimported: list[vault.InboundLink]
    bound: frozenset[str]


def inbound_links(vault_root: Path, rel: str) -> InboundCheck:
    """The links to the stored page ``rel``, from the store and from the files, each counted once.

    Neither source sees the whole vault. The graph rows are derived in each commit, so they name
    the linking pages as of the current revision, including a page that has no file yet; they hold
    nothing for a note that nothing has imported. The file index finds a note outside the store's
    pages, as file mode does, and misses a page that has no file. A page that the store holds is
    read from the store, and its file is only a projection, which may trail it. A graph source with
    no stored page comes from the index or, failing that, is reported at line 0 (a link by title or
    alias), never dropped. A file in the Knowledge Base that the store holds no page for is read
    off the disk, because the index reads it through the store and skips it; a bare ``[[name]]``
    in it counts even when another page shares the name. A file that a queued delete has not
    removed yet counts until the projector catches up. Each wrong count costs one ``force_orphan``.
    ``bound`` is every graph source: the commit refuses a source outside it.
    """
    forms = set(vault._inbound_target_forms(rel)[1:])
    # The stored page may have no file yet, and the index reads a bare `[[name]]` by counting files.
    on_disk = vault.find_inbound_wikilinks(
        vault_root, rel, target_has_no_file=not (Path(vault_root) / rel).exists()
    )

    def matching(source: str, text: str) -> list[vault.InboundLink]:
        return [
            vault.InboundLink(source, line, context, raw)
            for line, context, raw in vault._scan_wikilinks(text)
            if vault._inbound_bucket_key(raw) in forms
        ]

    links: list[vault.InboundLink] = []
    unimported: list[vault.InboundLink] = []
    with read.snapshot(vault_root) as view:
        sources = view.inbound_sources(rel)
        for source in sorted(sources):
            page = view.page(source)
            if page is not None:
                text = page.body.decode("utf-8", errors="replace")
                links.extend(matching(source, text) or [vault.InboundLink(source, 0, "", "")])
        links.extend(link for link in on_disk if view.page_meta(link.path) is None)
        for path in vault.walk_vault_md(vault_root):
            source = vault.relative_posix(path, Path(vault_root))
            if (
                ownership.classify(source) is ownership.Ownership.PAGE
                and view.page_meta(source) is None
            ):
                try:
                    # The descriptor read refuses a symlink, which a file swapped after the walk
                    # could be; `read_guarded_text` would look for the file in the store instead.
                    raw, _guard = vault._read_bounded_guarded_snapshot(
                        Path(vault_root), source, path.lstat().st_size
                    )
                except (OSError, vault.PathGuardError):
                    continue
                unimported.extend(matching(source, raw.decode("utf-8", errors="replace")))
    shown = {link.path for link in links}
    links.extend(vault.InboundLink(source, 0, "", "") for source in sorted(sources - shown))
    return InboundCheck(links, unimported, frozenset(sources))


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
