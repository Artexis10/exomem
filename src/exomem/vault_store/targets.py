"""Where a store-mode writer looks for its page, and what it refuses (store task 2.2).

A page that a commit stored has no file until the projector writes it, so a writer that asks the
disk whether its source exists, or whether its destination is free, answers for the projection and
not for the vault. These helpers answer from the store for the paths that the store owns, and from
the disk for the rest. A writer whose store form does not exist yet refuses through the same place.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
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
        return "only a Markdown page is held by the store, and a file's bytes stay on disk"
    if lifecycle.policy_governs_anything(vault_root):
        return "a governance policy is declared, and its tombstones and receipts go to files"
    # The folder is a dot folder, on disk, except for a page marked raw, whose parts are pages.
    parts_folder = extraction_parts.folder_for(Path(rel)).as_posix()
    if (
        paired
        or "stored_object" in fm
        or extraction_parts.count_of(fm) > 1
        or (Path(vault_root) / parts_folder).is_dir()
        or read.holds_folder(vault_root, parts_folder)
    ):
        return "its stored original or its part pages move with it, and they have no store form"
    return None


def active_catalog_gap(
    vault_root: Path,
    operation: str,
    *,
    error_class: Callable[..., Exception],
    writes: tuple[vault.PlannedWrite, ...] = (),
    removals: tuple = (),
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
            vault_root, writes=writes, removals=removals, now=int(time.time())
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

    It prevents the same disk write as ``refuse_unrouted`` and is the form for a writer that has no
    path to test before it starts, so it costs a rollback to run that writer on files.
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
