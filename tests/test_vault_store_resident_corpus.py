"""A store-mode save leaves the corpus and the resolvers resident, and they answer as a cold build does.

OpenSpec make-vault-store-canonical, task 2.2. The commit moves the corpus context and the resolvers
forward by the pages that it names (``find.on_store_commit``). That is only worth its saving if the
moved state equals the state that a build from nothing gives, for the pages that the store holds and for
the files that it does not own. The cost gate is ``test_vault_store_write_cost``.
"""

from __future__ import annotations

import os
from contextlib import closing

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401  (the fixture that `stored` builds on)
from test_vault_store_write_cost import create, edit, grow

from exomem import commands, find, freshness, semantic_contract
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.vault_store import mode, projector, read, resident_catalogue, write

stored, writer, served = read_write.stored, read_write.writer, write_path.served

KB = "Knowledge Base"


def keep_file(root, rel, text="# A file the store does not own\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def full_build(root, monkeypatch):
    """The corpus built from nothing, as an installation with the cache off builds it."""
    with monkeypatch.context() as scoped:
        scoped.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
        return semantic_contract.build_corpus_context(root)


def resident_build(root, monkeypatch):
    """The corpus a reader gets after a save, and how many walks of the vault it took."""
    walks = []
    census = semantic_contract._corpus_census

    def counting(*args, **kwargs):
        walks.append(args)
        return census(*args, **kwargs)

    monkeypatch.setattr(semantic_contract, "_corpus_census", counting)
    context = semantic_contract.build_corpus_context(root)
    monkeypatch.setattr(semantic_contract, "_corpus_census", census)
    return context, len(walks)


def maps(resolver):
    return (
        frozenset(resolver.full_paths), frozenset(resolver.kb_stripped), dict(resolver.stems),
        dict(resolver.titles), dict(resolver.candidates_by_key),
    )


def drained_disk_resolver(root, writer):
    """A resolver over what the disk holds once the projector has written every page the store holds."""
    for _ in range(10):
        projector.Projector(root, run=lambda work: work(writer)).run_pass()
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            if not reader.execute("SELECT path FROM store_projection_queue").fetchall():
                break
    entries = []
    for path in vault_module.walk_vault_md(root):
        entries.append((vault_module.relative_posix(path, root), vault_module.WikilinkResolver.read_title(path)))
    return vault_module.WikilinkResolver.from_entries(root, entries)


def move(root, path):
    """Rename a page inside its folder; the new path."""
    new_path = f"{path.rpartition('/')[0]}/Moved Probe.md"
    commands.op_move_file(root, path, new_path)
    return new_path


def delete(root, path):
    commands.op_delete(root, path, confirm=True)


@pytest.fixture
def warm(served, writer, corpus_cache_on):  # noqa: F811
    """A vault of stored pages and the files that the store does not own, whose corpus a save has warmed."""
    grow(writer, 1, 70, on_disk=served)
    keep_file(served, f"{KB}/Records/Work/view.md", "---\ntitle: Work view\n---\n# Work view\n")
    keep_file(served, "Reference/Manual.md", "---\ntitle: The manual\n---\n# The manual\n")
    edit(served, create(served, "Warm-up"))
    return served


@pytest.mark.parametrize("save", [
    pytest.param(lambda root, path: create(root, "Fresh page"), id="create"),
    pytest.param(edit, id="edit"),
    pytest.param(move, id="move"),
    pytest.param(delete, id="delete"),
])
def test_the_corpus_a_commit_advances_equals_a_build_from_nothing(warm, writer, monkeypatch, save):  # noqa: F811
    """Defect that this guards: a corpus that a commit moves forward by its pages drifts from the corpus of the
    vault (a page, a link, a resolver entry or an inbound list that the save changed and the move missed).
    After each kind of save, the corpus that the next reader gets, with no walk of the vault, equals the
    corpus built with the cache off."""
    path = create(warm, "Probe page")
    save(warm, path)
    resident, walks = resident_build(warm, monkeypatch)
    assert walks == 0, "the next reader walked the vault, so this proved nothing about the advance"
    assert resident.as_dict() == full_build(warm, monkeypatch).as_dict()
    # The census that the advance leaves is the one a walk gives: a drifted entry would send the next save to a reconcile.
    advanced, structural = semantic_contract._CORPUS_CONTEXT_CACHE[semantic_contract._corpus_cache_key(warm)]
    assert advanced == semantic_contract._corpus_census(warm)
    # `as_dict` leaves out the titles that the writer's resolver adds for paths with no page state here, and the
    # context that a reader gets carries none: the resident structural context holds them.
    with monkeypatch.context() as scoped:
        scoped.setenv("EXOMEM_DISABLE_CORPUS_CACHE", "1")
        cold = semantic_contract._build_corpus_context_with_census(warm)[0]
    assert dict(structural.unparsed_titles) == dict(cold.unparsed_titles)


def test_the_writers_resolver_equals_one_built_from_the_store_and_one_from_the_drained_disk(
    warm, writer, monkeypatch,  # noqa: F811
):
    """Defect that this guards: a writer's resolver taken from the resident corpus that misses a page that the
    store does not own (a Records view, a reference page), or holds a title that is not the page's, so that a new
    page's links resolve differently from file mode. It equals the resolver that the store and the disk's
    other files build, and the resolver over the disk after the projector has written every stored page."""
    path = create(warm, "Probe page")
    move(warm, path)
    edit(warm, create(warm, "Second probe"))
    resident = semantic_contract.current_store_writer_resolver(warm)
    assert resident is not None
    from_store = vault_module.WikilinkResolver(warm)
    kept = {f"{KB}/Records/Work/view", "Reference/Manual"}
    assert kept <= resident.full_paths, kept - resident.full_paths
    assert maps(resident) == maps(from_store)
    assert maps(resident) == maps(drained_disk_resolver(warm, writer))


def test_the_catalogue_resolver_a_commit_leaves_equals_one_built_from_the_catalogue_rows(warm, writer):  # noqa: F811
    """Defect that this guards: the next commit derives its links with a resolver that the last commit carried
    forward, which would resolve a link differently from one built over the catalogue if a delta were missed."""
    delete(warm, move(warm, create(warm, "Probe page")))
    edit(warm, create(warm, "Second probe"))
    with read.snapshot(warm) as view:
        held = resident_catalogue.held(warm, read.vault_triple(view.connection, view.revision))
    assert held is not None, "the last commit left no catalogue resolver at the store's triple"
    rows, resolver = held
    with closing(connection.open_reader(connection.store_path(warm))) as reader:
        assert rows == dict(reader.execute("SELECT path, title FROM pages WHERE in_vault = 1").fetchall())
    assert maps(resolver) == maps(vault_module.WikilinkResolver.from_entries(warm, rows.items()))


def keep_unreadable_files(root):
    """Two Markdown files outside the Knowledge Base that a corpus build cannot parse: one that is not
    UTF-8 text and one that the process cannot read. The locked file's path, whose mode the caller restores."""
    (root / "Reference" / "Latin.md").write_bytes(b"---\ntitle: Caf\xe9 notes\n---\n# Caf\xe9\n")
    locked = keep_file(root, "Reference/Locked.md", "---\ntitle: Locked\n---\n# Locked\n")
    locked.chmod(0)
    return locked


def put_trashed_page(writer, title):
    """A stored page in a Knowledge Base folder that the identity walk prunes."""
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "a page in the trash",
        puts=(write.PagePut(f"{KB}/trash/Old note.md", f"---\ntitle: {title}\n---\n# {title}\n".encode()),),
    ))


def test_the_writers_resolver_indexes_every_path_that_a_build_from_the_disk_does(warm, writer):  # noqa: F811
    """Defect that this guards: a writer's resolver that leaves out a Markdown path that the corpus build does not
    parse (a file that is not UTF-8 text, a file that the process cannot read, a stored page in the trash folder).
    A link to it then resolves differently from a build over the disk. It equals one, from a cold build and from a
    corpus that later commits moved forward."""
    locked = keep_unreadable_files(warm)
    try:
        put_trashed_page(writer, "Old note")
        semantic_contract.reset_corpus_context_cache()
        edit(warm, create(warm, "Rewarm"))
        semantic_contract.build_corpus_context(warm)
        resident = semantic_contract.current_store_writer_resolver(warm)
        assert resident is not None
        assert {"Reference/Latin", "Reference/Locked", f"{KB}/trash/Old note"} <= resident.full_paths
        assert maps(resident) == maps(vault_module.WikilinkResolver(warm))
        # The commit moves the corpus forward: the page's new title, and a second page in the trash.
        put_trashed_page(writer, "Old note, retitled")
        edit(warm, create(warm, "Another save"))
        moved = semantic_contract.current_store_writer_resolver(warm)
        assert moved is not None
        assert "old note, retitled" in moved.titles
        assert maps(moved) == maps(vault_module.WikilinkResolver(warm))
        # A governed delete moves the page into a folder that no build over the disk indexes.
        delete(warm, create(warm, "Lighthouse ledger"))
        deleted = semantic_contract.current_store_writer_resolver(warm)
        assert deleted is not None
        assert maps(deleted) == maps(vault_module.WikilinkResolver(warm))
    finally:
        locked.chmod(0o644)


def test_a_link_to_a_file_that_cannot_be_parsed_is_stored_as_a_build_over_the_disk_stores_it(warm, monkeypatch):  # noqa: F811
    """Defect that this guards: a remembered page whose link to a file outside the Knowledge Base is stored short
    (``[[Latin]]``) with an unresolved-link warning, because the writer's resolver left that file out, where a
    build over the disk stores the full path and warns of nothing. The saved page and its warnings are the same."""
    (warm / "Reference" / "Latin.md").write_bytes(b"---\ntitle: Caf\xe9 notes\n---\n# Caf\xe9\n")
    semantic_contract.reset_corpus_context_cache()
    edit(warm, create(warm, "Rewarm"))
    semantic_contract.build_corpus_context(warm)

    def remember(title):
        saved = commands.op_remember(
            warm, title=title, summary="A page that links the old file.",
            content="## Observations\n\n- [finding] See [[Latin]] for the old notes.\n",
            relation_disposition="reviewed_none", relation_review_reason="a link under test",
        )
        with read.snapshot(warm) as view:
            body = view.page(saved["path"]).body.decode()
        return [line for line in body.splitlines() if "Latin" in line], saved.get("warnings")

    resident = remember("Resident linker")
    with monkeypatch.context() as scoped:
        scoped.setattr(semantic_contract, "current_store_writer_resolver", lambda _root: None)
        from_disk = remember("Disk linker")
    assert resident == from_disk
    assert any("[[Reference/Latin]]" in line for line in resident[0]), resident


@pytest.mark.parametrize(("rel", "mode_before", "change"), [
    pytest.param("Reference/Readable.md", 0o644, lambda path: path.chmod(0), id="a-page-the-process-can-no-longer-read"),
    pytest.param("Reference/Locked.md", 0, lambda path: path.chmod(0o644), id="a-page-the-process-can-read-again"),
    pytest.param("Reference/Manual.md", 0o644, lambda path: os.link(path, path.with_suffix(".bin")), id="a-page-that-gains-a-second-link"),
])
def test_a_change_to_a_files_mode_or_links_reaches_the_writers_resolver(warm, rel, mode_before, change):  # noqa: F811
    """Defect that this guards: a resident proof that compares the size and mtime of the files and so misses a
    change that neither moves (a page's permissions, or a second hard link) but that decides whether a build
    over the disk reads its title or indexes it at all. After each change, with no commit and no event, the
    writer's resolver equals a build over the disk, before and after the next build of the corpus."""
    path = keep_file(warm, rel, "---\ntitle: Distinctive Manual\n---\n# Manual\n")
    path.chmod(mode_before)
    try:
        semantic_contract.reset_corpus_context_cache()
        edit(warm, create(warm, "Rewarm"))
        semantic_contract.build_corpus_context(warm)
        before = semantic_contract.current_store_writer_resolver(warm)
        assert before is not None, "the resident route was not live, so this proved nothing about its proof"
        assert maps(before) == maps(vault_module.WikilinkResolver(warm))
        change(path)
        assert maps(find.writer_resolver_snapshot(warm)) == maps(vault_module.WikilinkResolver(warm))
        # The build that the save runs next reconciles the corpus with the change; the resolver that it leaves
        # resident for the save after it is the disk's too.
        semantic_contract.build_corpus_context(warm)
        assert maps(find.writer_resolver_snapshot(warm)) == maps(vault_module.WikilinkResolver(warm))
    finally:
        path.chmod(0o644)


@pytest.mark.parametrize("imported", [True, False], ids=["importer-refresh-commit", "no-commit-yet"])
def test_an_outside_edit_to_a_file_the_store_does_not_own_reaches_the_corpus_while_the_registry_is_live(  # noqa: F811
    warm, writer, monkeypatch, imported,
):
    """Defect that this guards: a managed runtime's warm-up seeds the freshness registry and a store-mode watcher
    never feeds it, so a corpus whose event caption survived a commit read as current for ever: an outside edit to
    a file that the store does not own (a reference page) was never seen again. The build after the importer's
    commit, and a governed create and edit after that, serve the file's new title, as a build from nothing does."""
    assert all(freshness.rebaseline(warm).values())
    semantic_contract.reset_corpus_context_cache()
    semantic_contract.build_corpus_context(warm)  # the warm-up's cold build, captioned against the live registry
    page = "Reference/Manual.md"
    keep_file(warm, page, "---\ntitle: The renamed handbook\n---\n# The renamed handbook\n")
    if imported:
        # What the store-mode importer commits for an outside edit to a file that it does not own.
        write.commit(writer, write.StoreChange(
            write.Origin.OUTSIDE_EDITOR, "an outside change to a file that the store does not own", refresh=(page,),
        ))
        assert semantic_contract.build_corpus_context(warm).pages[page].title == "The renamed handbook"
    edit(warm, create(warm, "A later governed save"))
    later = semantic_contract.build_corpus_context(warm)
    assert later.pages[page].title == "The renamed handbook"
    assert later.as_dict() == full_build(warm, monkeypatch).as_dict()
    resolver = semantic_contract.current_store_writer_resolver(warm)
    assert resolver is not None
    assert maps(resolver) == maps(vault_module.WikilinkResolver(warm))


def catalogue_triple(root):
    with read.snapshot(root) as view:
        return read.vault_triple(view.connection, view.revision)


def test_the_resident_catalogue_resolver_is_bounded_and_a_stale_one_is_released(tmp_path):
    """Defect that this guards: a resolver over every catalogue row (megabytes per vault) that the process keeps
    for ever, for each vault that it ever committed to and after the triple it stands at has passed. The
    map keeps the vaults that a commit used last, and a lookup at another triple releases the entry."""
    triple, other = (1, 1, "store:1:1"), (1, 2, "store:2:1")
    roots = [tmp_path / name for name in ("first", "second", "third")]
    for root in roots:
        resident_catalogue.remember(root, triple, ({}, vault_module.WikilinkResolver.from_entries(root, [])))
    assert [resident_catalogue.held(root, triple) is not None for root in roots] == [False, True, True]
    assert resident_catalogue.held(roots[1], other) is None
    assert resident_catalogue.held(roots[1], triple) is None, "the stale entry stayed held"
    resident_catalogue.forget()
    assert resident_catalogue.held(roots[2], triple) is None


@pytest.mark.parametrize("release", [
    pytest.param(lambda root, scratch: mode.forget_read_mode(root), id="the-vault-leaves-store-mode"),
    pytest.param(lambda root, scratch: find.unload_ram_caches(pages=True), id="an-idle-process-hands-memory-back"),
])
def test_the_resident_catalogue_resolver_goes_when_its_owner_does(warm, tmp_path, release):  # noqa: F811
    """Defect that this guards: a vault that left store mode, or a process that went idle, that still holds the
    resolver that its last commit left."""
    edit(warm, create(warm, "Probe page"))
    triple = catalogue_triple(warm)
    assert resident_catalogue.held(warm, triple) is not None, "the commit left no resolver, so this proved nothing"
    release(warm, tmp_path)
    assert resident_catalogue.held(warm, triple) is None


def full_walk(root, monkeypatch):
    """The disk census with no store: every Markdown file that either production walk reads."""
    with monkeypatch.context() as scoped:
        scoped.setattr(semantic_contract, "_store_census_owns", lambda _root: None)
        return semantic_contract._disk_page_census(root)


def stored_paths(root):
    with read.snapshot(root) as view:
        return {meta.path for meta in view.pages()}


def test_the_disk_census_of_a_store_vault_is_the_full_walk_less_the_stored_pages(warm, monkeypatch):  # noqa: F811
    """Defect that this guards: a census that leaves out a file that the store does not own (a Records view, a
    reference page, a sync-conflict copy, a dot-named file, a file outside the Knowledge Base), so that an edit
    to it never reaches the corpus. It is the full walk of the disk less the files that the store holds."""
    keep_file(warm, f"{KB}/Notes/Note 2.sync-conflict-20260101-120000-ABCDEFG.md")
    keep_file(warm, f"{KB}/Notes/.hidden.md")
    keep_file(warm, "Loose.md")
    census = semantic_contract._disk_page_census(warm)
    walked = full_walk(warm, monkeypatch)
    held = stored_paths(warm)
    assert census is not None and walked is not None
    assert held & {entry[0] for entry in walked}, "the vault must hold stored pages whose files the census leaves out"
    assert census == frozenset(entry for entry in walked if entry[0] not in held)
    for rel in (f"{KB}/Records/Work/view.md", "Reference/Manual.md", "Loose.md"):
        assert rel in {entry[0] for entry in census}


def test_an_alias_below_the_knowledge_base_still_refuses_the_census(warm):  # noqa: F811
    """Defect that this guards: an alias that the corpus walk refuses (it can hide or duplicate pages) that the
    census vouches for, so that a cached corpus outlives it. The census stays as strict as the walk it mirrors,
    in a folder that the store owns too."""
    alias = warm / KB / "Notes" / "Linked"
    alias.symlink_to(warm / "Reference", target_is_directory=True)
    assert semantic_contract._disk_page_census(warm) is None
    alias.unlink()
    assert semantic_contract._disk_page_census(warm) is not None
