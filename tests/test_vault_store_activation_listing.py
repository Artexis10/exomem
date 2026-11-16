"""What the activation index and the context catalogue list in a store-mode vault.

A page is listed as soon as its commit returns, not when the projector writes its file. The vault
is the migration suite's generated vault, in store mode, with no projector: a committed page has no
file until a test writes one.
"""

from __future__ import annotations

from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, vault  # noqa: F401
from test_vault_store_read_write import NEW, put, stored, writer  # noqa: F401

from exomem import context_catalogue, find, working_set_index, working_set_runtime
from exomem.vault_store import write

ARCHIVED = "Knowledge Base/_archive/Gamma Old.md"
TRASHED = "Knowledge Base/_trash/Gamma Gone.md"
PAGE = b"---\ntitle: Gamma\ntype: note\n---\n# Gamma\n\nA page that only the store holds.\n"
ALPHA_WITH_ALIAS = (
    b"---\ntitle: Alpha\ntype: note\naliases: [Alpha Prime]\n---\n# Alpha\n\n"
    b"An edit that only the store holds.\n"
)


def current_index(root):
    """The index as recall_context gets it: refreshed to the stamp of the vault's current state."""
    stamp = working_set_runtime._key_text(find.FreshnessSnapshot(root).projection_key("kb"))
    state, index, stale = working_set_runtime.ensure_index(root, freshness_stamp=stamp)
    assert (state, stale) == (working_set_runtime.READY, False)
    return index, stamp


def test_a_page_is_listed_as_soon_as_it_is_committed(stored, writer):  # noqa: F811
    """A profile or a map omitted a just-saved page while it reported ready and current: the index
    listed pages by walking the disk, and the page's file did not exist yet."""
    stamp = current_index(stored)[1]
    # The catalogue is warm, as a running service holds it.
    context_catalogue.ensure_unlisted(stored, freshness_stamp=stamp)
    write.commit(writer, put(NEW, PAGE))
    assert not (stored / NEW).exists()

    index, stamp = current_index(stored)
    assert index.resolve_names(["Gamma"]) == {"gamma": (NEW,)}
    _state, catalogue, _unlisted = context_catalogue.ensure_unlisted(stored, freshness_stamp=stamp)
    assert NEW in {entry.path for entry in catalogue.entries()}


def test_an_edit_that_only_the_store_holds_replaces_the_entry_that_the_old_file_gave(
    stored, writer, monkeypatch  # noqa: F811
):
    """The old file keeps its stat, so a stored page keyed on the file's stat would reuse the entry
    that the old bytes gave, in the index and in the catalogue."""
    # The fixture's files are seconds old, inside the window that stops a file's entry being reused
    # (the window reads the change time too, which back-dating a file cannot move).
    monkeypatch.setattr(working_set_index, "_RACY_WINDOW_NS", 0)
    index, stamp = current_index(stored)
    assert index.resolve_names(["Alpha Prime"]) == {}
    context_catalogue.ensure_unlisted(stored, freshness_stamp=stamp)
    write.commit(writer, put(ALPHA, ALPHA_WITH_ALIAS))
    assert b"Alpha Prime" not in (stored / ALPHA).read_bytes()

    index, stamp = current_index(stored)
    assert index.resolve_names(["Alpha Prime"]) == {"alpha prime": (ALPHA,)}
    _state, catalogue, _unlisted = context_catalogue.ensure_unlisted(stored, freshness_stamp=stamp)
    [entry] = [entry for entry in catalogue.entries() if entry.path == ALPHA]
    assert "An edit that only the store holds." in entry.description


def test_the_index_lists_a_stored_page_by_the_walks_folder_rule(stored, writer):  # noqa: F811
    """An archived page stays listed, as its file would be, and a page under a skipped folder stays
    out. The find corpus's folder rule would drop the first."""
    write.commit(writer, put(ARCHIVED, PAGE))
    write.commit(writer, put(TRASHED, PAGE.replace(b"Gamma", b"Gamma Gone")))

    names = current_index(stored)[0].resolve_names(["Gamma", "Gamma Gone"])
    assert names == {"gamma": (ARCHIVED,)}
