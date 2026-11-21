"""A store-mode save leaves the corpus and the resolvers resident, and they answer as a cold build does.

OpenSpec make-vault-store-canonical, task 2.2. The commit moves the corpus context and the resolvers
forward by the pages that it names (``find.on_store_commit``). That is only worth its saving if the
moved state equals the state that a build from nothing gives, for the pages that the store holds and for
the files that it does not own. The cost gate is ``test_vault_store_write_cost``.
"""

from __future__ import annotations

from contextlib import closing

import pytest
import test_vault_store_read_write as read_write
import test_vault_store_write_path as write_path
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_write_cost import create, edit, grow

from exomem import commands, semantic_contract
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.vault_store import derive, projector, read

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
        entries.append((vault_module.relative_posix(path, root), vault_module.WikilinkResolver._read_title(path)))
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
    advanced = semantic_contract._CORPUS_CONTEXT_CACHE[semantic_contract._corpus_cache_key(warm)][0]
    assert advanced == semantic_contract._corpus_census(warm)


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
    _triple, rows, resolver = derive._CATALOGUE[warm.resolve()]
    with closing(connection.open_reader(connection.store_path(warm))) as reader:
        assert rows == dict(reader.execute("SELECT path, title FROM pages WHERE in_vault = 1").fetchall())
    assert maps(resolver) == maps(vault_module.WikilinkResolver.from_entries(warm, rows.items()))


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
