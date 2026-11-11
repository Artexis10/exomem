"""Where a store-mode writer looks for its page, and what it refuses (store task 2.2).

A page that a commit stored has no file until the projector writes it, so a writer that asks the
disk whether its source exists, or whether its destination is free, answers for the projection and
not for the vault. These helpers answer from the store for the paths that the store owns, and from
the disk for the rest. A writer whose store form does not exist yet refuses through the same place.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Collection, Iterable, Mapping
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
    assert isinstance(resolution, vault.VaultPathResolution)
    rel = resolution.relative
    withheld = vault._write_target_withheld(
        vault_root, resolution.resolved, Path(vault_root).resolve()
    )
    if withheld or not holds(vault_root, rel):
        raise vault.VaultPathError(code="NOT_FOUND", reason=vault.missing_path_reason(rel))
    if resolution.candidate.exists() and not resolution.candidate.is_file():
        raise vault.VaultPathError(code="NOT_A_FILE", reason=f"path is not a regular file: {rel}")
    return resolution.candidate, rel


class InboundCheck(NamedTuple):
    """The links to a stored page, and the linking pages that its delete commit must bind.

    ``unimported`` holds the links from files in the Knowledge Base that the store has no page
    for. No policy covers such a file yet, so the publication filter would hide it from every
    caller. The caller decides whether to count it, and must not name it to a restricted one.
    """

    links: list[vault.InboundLink]
    unimported: list[vault.InboundLink]
    bound: frozenset[str]


def inbound_links(vault_root: Path, rels: Iterable[str]) -> dict[str, InboundCheck]:
    """The links to each stored page in ``rels``, from the store and from the files, each counted
    once.

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

    A folder's pages share one walk of the disk, one read of the file index and one read of the
    graph rows, so the cost grows with the links and not with the pages times the vault.

    The graph keeps no edge to a binary, which is no page. A binary's linking pages are the stored
    pages whose recorded link targets name it (``epistemic_graph.dependent_paths``), each read
    once; ``bound`` holds no source for it, so its commit binds no link gained after this read.
    """
    from .. import epistemic_graph

    root = Path(vault_root)
    targets = list(dict.fromkeys(rels))
    files = [rel for rel in targets if ownership.classify(rel) is not ownership.Ownership.PAGE]
    # A form names the pages that a link in that form reaches: two pages may share a basename.
    reached: dict[str, set[str]] = {}
    for rel in targets:
        for form in vault._inbound_target_forms(rel)[1:]:
            reached.setdefault(form, set()).add(rel)

    def matching(source: str, text: str) -> dict[str, list[vault.InboundLink]]:
        found: dict[str, list[vault.InboundLink]] = {}
        for line, context, raw in vault._scan_wikilinks(text):
            for rel in reached.get(vault._inbound_bucket_key(raw), ()):
                if rel != source:
                    found.setdefault(rel, []).append(vault.InboundLink(source, line, context, raw))
        return found

    # A stored page may have no file yet, and the index reads a bare `[[name]]` by counting files.
    on_disk = vault.find_inbound_wikilinks_many(root, targets, held=True)
    links: dict[str, list[vault.InboundLink]] = {rel: [] for rel in targets}
    unimported: dict[str, list[vault.InboundLink]] = {rel: [] for rel in targets}
    with read.snapshot(root) as view:
        sources = view.inbound_sources_many(targets)
        naming = (
            epistemic_graph.dependent_paths(view.connection, files, ()) if files else set()
        )
        # One chunk of linking pages' bodies at a time: a folder's linkers may be thousands.
        linkers = sorted(set().union(*sources.values()) | naming)
        found_in: dict[str, dict[str, list[vault.InboundLink]]] = {}
        for start in range(0, len(linkers), read.KEYS_PER_QUERY):
            for source, body in view.bodies(linkers[start : start + read.KEYS_PER_QUERY]).items():
                found_in[source] = matching(source, body.decode("utf-8", errors="replace"))
        for rel in targets:
            for source in sorted(sources[rel] & found_in.keys()):
                links[rel].extend(
                    found_in[source].get(rel) or [vault.InboundLink(source, 0, "", "")]
                )
        for rel in files:
            for source in sorted(naming & found_in.keys()):
                links[rel].extend(found_in[source].get(rel, ()))
        indexed = view.held_pages(link.path for found in on_disk.values() for link in found)
        for rel, found in on_disk.items():
            links[rel].extend(link for link in found if link.path not in indexed)
        candidates: dict[str, Path] = {}
        for path in vault.walk_vault_md(root):
            source = vault.relative_posix(path, root)
            if ownership.classify(source) is ownership.Ownership.PAGE:
                candidates[source] = path
        stored = view.held_pages(candidates)
    for source, path in candidates.items():
        if source in stored:
            continue
        try:
            # The descriptor read refuses a symlink, which a file swapped after the walk could be;
            # `read_guarded_text` would look for the file in the store instead.
            raw, _guard = vault._read_bounded_guarded_snapshot(root, source, path.lstat().st_size)
        except (OSError, vault.PathGuardError):
            continue
        for rel, found in matching(source, raw.decode("utf-8", errors="replace")).items():
            unimported[rel].extend(found)
    checks: dict[str, InboundCheck] = {}
    for rel in targets:
        shown = {link.path for link in links[rel]}
        links[rel].extend(
            vault.InboundLink(source, 0, "", "") for source in sorted(sources[rel] - shown)
        )
        checks[rel] = InboundCheck(links[rel], unimported[rel], sources[rel])
    return checks


def visible_links(
    vault_root: Path, checks: Mapping[str, InboundCheck]
) -> dict[str, list[vault.InboundLink]]:
    """The links of each check that the calling writer may count and name.

    A link from a page withheld from the caller, by a policy or by RAW, is dropped, so the writer
    orphans it as ``force_orphan`` would, and the owner's audit reports it. An unimported file has
    no policy, and the store's filter withholds it from every reader, so to a restricted caller it
    is absent: no count, no path. Only the owner counts it.
    """
    from ..governance import egress

    visible = egress.restricted_release_filter(vault_root)
    owner = caller_is_owner(vault_root)
    return {
        rel: [link for link in check.links if visible is None or visible(link.path)]
        + (check.unimported if owner else [])
        for rel, check in checks.items()
    }


def caller_is_owner(vault_root: Path) -> bool:
    """Whether nothing is withheld from the calling writer, by a policy or by RAW.

    A store-mode writer asks this, not whether ``restricted_release_filter`` returned a filter: a
    bound store filters every caller, the owner too. The principal and its authority decide it, as
    they do in file mode. Only the owner hears of a file that the store has not imported, and only
    the owner reads a row report that may name a page withheld from others.
    """
    from ..governance import egress

    return egress.file_mode_restricted_caller(vault_root, None) is None


#: The reasons a store form refuses, each named once for the page and the folder forms.
_LINKED = "the folder holds a symbolic link, which a folder write does not follow; remove it"
_NOT_HELD = (
    "the folder holds a Markdown file that the store holds no page for, and moving the folder "
    "would leave it behind; retry once the store has imported or removed it"
)


class FolderFiles(NamedTuple):
    """What sits below a folder besides its stored pages: what a folder write renames on disk.

    ``files`` are the binaries (recorded or not) and the files the store does not own; ``folders``
    are folders the store owns nothing in (a dot folder of parts), renamed whole. ``recorded`` is
    how many binary records the store holds below the folder, which the commit binds, and
    ``file_count`` how many files the renames move, the ones inside ``folders`` included.
    """

    files: tuple[str, ...]
    folders: tuple[str, ...]
    recorded: int
    file_count: int


def folder_files(
    vault_root: Path,
    view: read.StoreSnapshot,
    folder: str,
    pages: Collection[str],
    *,
    operation: str,
    error_class: Callable[..., Exception],
) -> FolderFiles:
    """The files and folders below ``folder`` that move on disk beside its stored ``pages``.

    The binaries that the store records come from one range read of ``view``; the disk is listed
    by name once, and only names are read. A Markdown file there is a stored page's projection,
    which moves with its page, or a file that the store holds no page for: a note that nothing
    has imported yet, or a page's old file that the projector has not removed yet. Moving the
    folder would leave it behind while the call reports the folder moved, so it refuses as
    ``operation``'s unrouted error; a wrong refusal lasts until the importer or the projector
    runs. A symbolic link below the folder raises ``error_class`` with RESERVED_PATH, as file
    mode's folder read refuses one: neither form follows a link out of the folder or leaves it
    behind.
    """
    root = Path(vault_root)
    recorded = [meta.path for meta in view.binaries(f"{folder}/")]
    files: set[str] = set(recorded)
    folders: list[str] = []
    unheld = False
    inside = 0
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            entries = list(os.scandir(root / current))
        except OSError:
            continue  # as `os.walk` does: a folder that cannot be listed names nothing
        for entry in entries:
            rel = f"{current}/{entry.name}"
            if entry.is_symlink():
                raise error_class(code="RESERVED_PATH", reason=_LINKED)
            if entry.is_dir(follow_symlinks=False):
                if ownership.owns_folder(rel):
                    pending.append(rel)
                else:
                    folders.append(rel)
                    inside += sum(len(names) for _dir, _dirs, names in os.walk(entry.path))
            elif ownership.classify(rel) is ownership.Ownership.PAGE:
                unheld = unheld or rel not in pages
            else:
                files.add(rel)
    if unheld:
        raise availability.unrouted(operation, _NOT_HELD)
    return FolderFiles(
        tuple(sorted(files)), tuple(sorted(folders)), len(recorded), len(files) + inside
    )


def active_catalog_gap(
    vault_root: Path,
    operation: str,
    *,
    error_class: Callable[..., Exception],
    writes: tuple[vault.PlannedWrite, ...] = (),
    removals: tuple = (),
    content_paths: tuple[str, ...] = (),
) -> None:
    """Refuse a store-mode lifecycle write while the vault's catalog publication is active.

    The catalog publishes a removal or a write in files after the change commits, and the store has
    no form for them, so a change that committed would leave the catalog naming pages that moved.
    It prevents that, and a wrong refusal costs a rollback of store mode. It is the same call that
    file mode makes before it changes anything, with no log or graph writes: they matter only to a
    publication that the store never carries, and an enrolled vault is refused with or without them.
    ``error_class`` is the writer's own error, raised with the catalog's code when it cannot tell.
    """
    from ..governance import catalog_publication

    try:
        target = catalog_publication.prepare_catalog_membership_batch(
            vault_root,
            writes=writes,
            removals=removals,
            content_paths=content_paths,
            now=int(time.time()),
        )
    except catalog_publication.CatalogPublicationError as error:
        raise error_class(
            code="GOVERNANCE_CATALOG_PUBLICATION_BLOCKED", reason=str(error)
        ) from error
    if target is not None:
        raise availability.unrouted(
            operation, "the vault's active catalog publication has no store form"
        )


def refuse_in_store_mode(vault_root: Path, operation: str, why: str) -> None:
    """Refuse ``operation`` while the vault is in store mode, for a writer whose paths are pages.

    It is the form for a writer that has no path to test with ``owns_any`` before it starts, so it
    costs a rollback to run that writer on files.
    """
    if mode.store_mode_active(vault_root):
        raise availability.unrouted(operation, why)


def relative(vault_root: Path, path: str) -> str:
    """The vault-relative form of a path, or ``VaultPathError`` when it does not resolve."""
    resolution = vault.resolve_under_vault(vault_root, path, return_details=True)
    assert isinstance(resolution, vault.VaultPathResolution)
    return resolution.relative


#: Why a store-mode move refuses an end that the store does not own.
_CROSSES = "a move between a path that the store holds and one it does not has no store form"
#: Why a store-mode move refuses to change what kind of file the store holds at its ends.
_KIND_CHANGES = (
    "a move between a page's path and a binary's path has no store form: the store holds a "
    "page's bytes, and a binary's stay on disk"
)


def refuse_kind_change(operation: str, source: str, destination: str) -> None:
    """Refuse a store-mode move whose ends the store holds as different kinds of file.

    A page moves in the store and a binary on disk, so neither form turns one into the other (a
    schema overlay file taken to the trash, say). It reads nothing: a path's owner is a rule on
    its name.
    """
    if ownership.classify(source) is not ownership.classify(destination):
        raise availability.unrouted(operation, _KIND_CHANGES)


def refuse_crossing(operation: str, rels: Iterable[str]) -> None:
    """Refuse a store-mode move with an end at a vault-relative path that the store does not own.

    The store form moves a stored page from one path that the store owns to another. The commit
    refuses any other path, but only after the preflight has judged the move and, for a recovery,
    after its relation-review slots have committed. So a writer refuses here first, and its
    validation refuses what its commit would. It reads nothing: a path's owner is a rule on its name.
    """
    if any(ownership.classify(rel) is ownership.Ownership.NONE for rel in rels):
        raise availability.unrouted(operation, _CROSSES)


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
            ownership.classify(relative(vault_root, path)) is not ownership.Ownership.NONE
            for path in files
        ) or any(ownership.owns_folder(relative(vault_root, path)) for path in folders)
    except vault.VaultPathError:
        return False

