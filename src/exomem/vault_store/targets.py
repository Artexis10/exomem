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
    """
    root = Path(vault_root)
    targets = list(dict.fromkeys(rels))
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
        found_in = {
            source: matching(source, body.decode("utf-8", errors="replace"))
            for source, body in view.bodies(set().union(*sources.values())).items()
        }
        for rel in targets:
            for source in sorted(sources[rel] & found_in.keys()):
                links[rel].extend(
                    found_in[source].get(rel) or [vault.InboundLink(source, 0, "", "")]
                )
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


#: The reasons a page-only store form refuses, each named once for the page and the folder forms.
_NOT_A_PAGE = "only a Markdown page is held by the store, and a file's bytes stay on disk"
_GOVERNED = "a governance policy is declared, and its tombstones and receipts go to files"
_CARRIES_MORE = "its stored original or its part pages move with it, and they have no store form"
_FILES_ON_DISK = "the folder holds files whose bytes stay on disk, and they have no store form"
_NOT_HELD = (
    "the folder holds a Markdown file that the store holds no page for, and moving the folder "
    "would leave it behind; retry once the store has imported or removed it"
)


def _carries_more(fm: Mapping[str, object]) -> bool:
    """Whether a page's frontmatter names an original or further parts that move with the page."""
    from .. import extraction_parts

    return "stored_object" in fm or extraction_parts.count_of(fm) > 1


def page_only_form_gap(
    vault_root: Path,
    rel: str,
    fm: dict,
    *,
    paired: bool = False,
    destination: str | None = None,
) -> str | None:
    """Why a store-mode vault cannot move this file yet, or None when it has a store form.

    The store form of a delete, a move or a recovery moves pages and nothing else. Each case below
    has a file-mode step that moves something the store has no form for, so the writer refuses
    before it changes anything. ``paired`` says that the writer found an original that moves with
    the page; ``destination`` is where a move puts it.
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
        or _carries_more(fm)
        or (Path(vault_root) / parts_folder).is_dir()
        or read.holds_folder(vault_root, parts_folder)
    ):
        return _CARRIES_MORE
    return None


def folder_form_gap(
    vault_root: Path, folder: str, pages: Mapping[str, Mapping[str, object]]
) -> str | None:
    """Why a store-mode vault cannot move this folder of pages yet, or None when it has a store form.

    ``pages`` maps every page that the store holds below ``folder`` to its frontmatter. The folder's
    store form moves those pages and nothing else, so a folder that also holds a file whose bytes
    stay on disk, or a page that carries its original or its part pages, refuses before anything
    changes. The disk is listed by name once, and only names are read: a Markdown file there is a
    stored page's projection, which moves with its page, or a file that the store holds no page
    for. Such a file is a note that nothing has imported yet, or a page's old file that the
    projector has not removed yet. It refuses, because the move would leave it behind while the
    call reports the folder moved. A wrong refusal lasts until the importer or the projector runs.
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
    for current, _folders, names in os.walk(root / folder):
        base = Path(current).relative_to(root).as_posix()
        for name in names:
            if ownership.classify(f"{base}/{name}") is not ownership.Ownership.PAGE:
                return _FILES_ON_DISK
            notes.add(f"{base}/{name}")
    if not notes <= pages.keys():
        return _NOT_HELD
    # A raw-marked page's parts are pages in a sibling folder, which is then one of these folders.
    held = {parent.as_posix() for rel in pages for parent in Path(rel).parents}
    if any(
        _carries_more(fm) or extraction_parts.folder_for(Path(rel)).as_posix() in held
        for rel, fm in pages.items()
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


def _relative(vault_root: Path, path: str) -> str:
    """The vault-relative form of a path, or ``VaultPathError`` when it does not resolve."""
    resolution = vault.resolve_under_vault(vault_root, path, return_details=True)
    assert isinstance(resolution, vault.VaultPathResolution)
    return resolution.relative


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
            ownership.classify(_relative(vault_root, path)) is not ownership.Ownership.NONE
            for path in files
        ) or any(ownership.owns_folder(_relative(vault_root, path)) for path in folders)
    except vault.VaultPathError:
        return False

