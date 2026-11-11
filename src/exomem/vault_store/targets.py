"""Where a store-mode writer looks for its page, and what it refuses (store task 2.2).

A page that a commit stored has no file until the projector writes it, so a writer that asks the
disk whether its source exists, or whether its destination is free, answers for the projection and
not for the vault. These helpers answer from the store for the paths that the store owns, and from
the disk for the rest. A writer whose store form does not exist yet refuses through the same place.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping
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
    """The links to each stored page in ``rels``: the store's inbound rows, then each file that the
    store holds no rows for, read off the disk.

    The rows answer for every page that the store holds and every file that the importer's sweep
    reached, as of the snapshot, by the file index's own rule (``vault.store_inbound_wikilinks``),
    with line and context: a page that has no file yet counts, and a file that a queued delete has
    not removed yet does not. A file with no rows (a note that nothing has imported or swept yet)
    is read off the disk, and a bare ``[[name]]`` in it counts even when another page shares the
    name. Such a note outside what the store owns counts as a link; one in the Knowledge Base goes
    to ``unimported``. Each wrong count costs one ``force_orphan``. ``bound`` is every page that
    the rows name: the commit binds the same rule (``write.check``) and refuses a link from a page
    outside it. A folder's pages share one read of the rows and one walk of the disk, so the cost
    grows with the links and not with the pages times the vault.
    """
    root = Path(vault_root)
    targets = list(dict.fromkeys(rels))
    # A form names the pages that a link in that form reaches: two pages may share a basename.
    reached: dict[str, set[str]] = {}
    for rel in targets:
        for form in vault._inbound_target_forms(rel)[1:]:
            reached.setdefault(form, set()).add(rel)
    unimported: dict[str, list[vault.InboundLink]] = {rel: [] for rel in targets}
    with read.snapshot(root) as view:
        links = vault.store_inbound_wikilinks(view.connection, targets)
        candidates = {vault.relative_posix(path, root): path for path in vault.walk_vault_md(root)}
        derived = vault.store_inbound_sources(view.connection, candidates)
    bound = {rel: frozenset(link.path for link in links[rel]) for rel in targets}
    for source, path in candidates.items():
        if source in derived:
            continue
        owned = ownership.classify(source) is ownership.Ownership.PAGE
        try:
            # The descriptor read refuses a symlink, which a file swapped after the walk could be;
            # `read_guarded_text` would look for the file in the store instead.
            raw, _guard = vault._read_bounded_guarded_snapshot(root, source, path.lstat().st_size)
        except (OSError, vault.PathGuardError):
            continue
        for line, context, target in vault._scan_wikilinks(raw.decode("utf-8", errors="replace")):
            for rel in reached.get(vault._inbound_bucket_key(target), ()):
                if rel != source:
                    (unimported if owned else links)[rel].append(
                        vault.InboundLink(source, line, context, target)
                    )
    return {rel: InboundCheck(links[rel], unimported[rel], bound[rel]) for rel in targets}


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


#: The reasons a page-only store form refuses, each named once for the page and the folder forms.
_NOT_A_PAGE = "only a Markdown page is held by the store, and a file's bytes stay on disk"
_GOVERNED = "a governance policy is declared, and its tombstones and receipts go to files"
_CARRIES_MORE = "its stored original or its part pages move with it, and they have no store form"
_FILES_ON_DISK = "the folder holds files whose bytes stay on disk, and they have no store form"
_LINKED = "the folder holds a symbolic link, which a folder write does not follow; remove it"
_NOT_HELD = (
    "the folder holds a Markdown file that the store holds no page for, and moving the folder "
    "would leave it behind; retry once the store has imported or removed it"
)


def _carries_more(body: bytes | None) -> bool:
    """Whether a page names an original or further parts that move with it.

    A page whose frontmatter does not parse names neither: the store form then moves its bytes.
    """
    from .. import extraction_parts

    try:
        fm = {} if body is None else vault.parse_frontmatter(body.decode("utf-8"))[0]
    except (UnicodeDecodeError, vault.FrontmatterError):
        fm = {}
    return "stored_object" in fm or extraction_parts.count_of(fm) > 1


def page_only_form_gap(
    vault_root: Path,
    rel: str,
    body: bytes | None,
    *,
    paired: bool = False,
    destination: str | None = None,
) -> str | None:
    """Why a store-mode vault cannot move this file yet, or None when it has a store form.

    The store form of a delete, a move or a recovery moves pages and nothing else. Each case below
    has a file-mode step that moves something the store has no form for, so the writer refuses
    before it changes anything. ``body`` is the page's exact bytes, or None for a file the store
    holds no page for. ``paired`` says that the writer found an original that moves with the page;
    ``destination`` is where a move puts it.
    """
    from .. import extraction_parts
    from ..governance import lifecycle

    # `.md` fixes the page format; any other file is a binary whose bytes stay on disk.
    if not all(path.lower().endswith(".md") for path in (rel, destination or rel)):
        return _NOT_A_PAGE
    if lifecycle.policy_governs_anything(vault_root):
        return _GOVERNED
    # The folder is a dot folder, on disk, except for a page marked raw, whose parts are pages.
    parts_folder = extraction_parts.folder_for(Path(rel)).as_posix()
    if (
        paired
        or _carries_more(body)
        or (Path(vault_root) / parts_folder).is_dir()
        or read.holds_folder(vault_root, parts_folder)
    ):
        return _CARRIES_MORE
    return None


def folder_form_gap(
    vault_root: Path,
    folder: str,
    pages: Mapping[str, bytes],
    *,
    error_class: Callable[..., Exception],
) -> str | None:
    """Why a store-mode vault cannot move this folder of pages yet, or None when it has a store form.

    ``pages`` maps every page that the store holds below ``folder`` to its exact bytes. The folder's
    store form moves those pages and nothing else, so a folder that also holds a file whose bytes
    stay on disk, or a page that carries its original or its part pages, refuses before anything
    changes. The disk is listed by name once, and only names are read: a Markdown file there is a
    stored page's projection, which moves with its page, or a file that the store holds no page
    for. Such a file is a note that nothing has imported yet, or a page's old file that the
    projector has not removed yet. It refuses, because the move would leave it behind while the
    call reports the folder moved. A wrong refusal lasts until the importer or the projector runs.
    A symbolic link below the folder raises ``error_class`` with RESERVED_PATH, as file mode's
    folder read refuses one: neither form follows a link out of the folder or leaves it behind.
    """
    from .. import extraction_parts
    from ..governance import lifecycle

    root = Path(vault_root)
    if lifecycle.policy_governs_anything(root):
        return _GOVERNED
    with read.snapshot(root) as view:
        if view.holds_binaries_under(f"{folder}/"):
            return _FILES_ON_DISK
    notes: set[str] = set()
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
                pending.append(rel)
            elif ownership.classify(rel) is not ownership.Ownership.PAGE:
                return _FILES_ON_DISK
            else:
                notes.add(rel)
    if not notes <= pages.keys():
        return _NOT_HELD
    # A raw-marked page's parts are pages in a sibling folder, which is then one of these folders.
    held = {parent.as_posix() for rel in pages for parent in Path(rel).parents}
    if any(
        _carries_more(body) or extraction_parts.folder_for(Path(rel)).as_posix() in held
        for rel, body in pages.items()
    ):
        return _CARRIES_MORE
    return None


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

