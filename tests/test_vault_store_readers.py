"""What a store-mode vault's readers answer when the projected Markdown cannot be trusted.

OpenSpec make-vault-store-canonical, design D3, task 3.1 (first half). The vault is the migration
suite's generated vault, moved into store mode; pages that only the store holds are committed
through ``write.commit``, as the store thread commits them.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, migrate_apply, vault  # noqa: F401
from test_vault_store_read_write import EDITED, NEW, put, stored, writer  # noqa: F401

from exomem import __main__ as cli
from exomem import find as find_module
from exomem import get_page, lexstore, list_directory, project_keys, warmup
from exomem import vault as vault_module
from exomem.vault_store import ownership, write
from exomem.vocabulary import registry

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)

SENTINEL = b"POISONED PROJECTION: a reader served this file instead of the store\n"
#: How many pages only the store holds; the drill at scale sets it from the environment.
GENERATED = int(os.environ.get("EXOMEM_READERS_DRILL_PAGES", "40"))


def generated_page(index):
    return (
        f"Knowledge Base/Notes/Gen/Page-{index:04d}.md",
        f"---\ntitle: Page {index}\ntype: note\n---\n# Page {index}\n\n"
        f"The lantern number {index} burns at the lighthouse harbour.\n".encode(),
    )


def corpus_pages(root):
    return {path.relative_to(root).as_posix() for path in find_module._corpus_walk(root, "kb")}


def answers(root, generated):
    """What find, get_page, a listing and the corpus say about the vault, as plain data."""
    hits = find_module.find(root, query="lighthouse", limit=15, graph=False, rerank=False)
    pages = [
        get_page.get_page(root, path=rel)
        for rel in (ALPHA, *(generated_page(index)[0] for index in generated))
    ]
    listing = list_directory.list_directory(root, path="Knowledge Base/Notes/Gen")
    return {
        "find": [(hit.path, hit.title, hit.excerpt) for hit in hits],
        "pages": [(page.content, page.content_hash) for page in pages],
        "listing": sorted(
            (entry.as_dict() for entry in listing.entries), key=lambda entry: entry["name"]
        ),
        "corpus": sorted(corpus_pages(root)),
    }


def poison_every_owned_page(root):
    for owned in ownership.iter_owned(root):
        if owned.ownership is ownership.Ownership.PAGE:
            (root / owned.rel).write_bytes(SENTINEL)


def test_find_get_page_and_listing_answer_from_the_store_when_every_projected_file_is_poisoned(
    stored, writer  # noqa: F811
):
    """A reader that still takes the file for the page: a stale projection, or one that a failed
    projector left half-written, reaches the person who asks, and the store's page does not."""
    for index in range(GENERATED):
        rel, body = generated_page(index)
        write.commit(writer, put(rel, body))
    sampled = range(0, GENERATED, max(1, GENERATED // 10))
    find_module.clear_cache()
    before = answers(stored, sampled)
    assert len(before["find"]) >= 2, "find must have answers for this check to mean anything"
    assert len(before["listing"]) == GENERATED
    assert len(before["corpus"]) >= GENERATED + 2

    poison_every_owned_page(stored)
    find_module.clear_cache()
    after = answers(stored, sampled)

    assert after == before
    assert SENTINEL.decode().strip() not in json.dumps(after)


def test_a_commit_that_changes_no_file_reaches_the_corpus_page_cache_and_resolver(
    stored, writer  # noqa: F811
):
    """A cache that keys on the file's stat never sees a commit that changes no file, so the corpus,
    the parsed page and a link keep missing the page that was just written."""
    assert NEW not in corpus_pages(stored)
    assert find_module._CACHE.get(stored / NEW, stored) is None
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) is None

    write.commit(writer, put(NEW, EDITED.replace(b"title: Alpha", b"title: Gamma")))

    assert not (stored / NEW).exists()
    assert NEW in corpus_pages(stored)
    assert find_module._CACHE.get(stored / NEW, stored).title == "Gamma"
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) == "gamma"

    write.commit(writer, put(NEW, EDITED.replace(b"title: Alpha", b"title: Delta")))

    assert find_module._CACHE.get(stored / NEW, stored).title == "Delta"
    assert find_module.shared_resolver(stored).title_key_for_path(NEW) == "delta"


def test_warm_and_status_leave_no_sidecar_and_status_reports_the_store(
    stored, capsys  # noqa: F811
):
    """A warm that builds the lexical sidecar from the projected files in a vault whose pages are
    in the store, or a status that reports nothing of the mode the vault is in."""
    sidecar = lexstore.lexical_path(stored)
    sidecar.unlink(missing_ok=True)
    present = {path for path in stored.rglob("*") if path.is_file()}

    assert warmup.warm_caches(stored, preload_models=False, preload_cpu_caches=True) is not None
    capsys.readouterr()
    assert cli._status_main(["--vault", str(stored), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert not sidecar.exists()
    assert {path for path in stored.rglob("*") if path.is_file()} == present
    assert report["vault_store"]["mode"] == "store"


def test_a_registry_overlay_that_only_the_store_holds_loads_and_follows_its_commits(
    stored, writer  # noqa: F811
):
    """An overlay loader that reads the file's stat takes an overlay with no file for none, so a
    store-mode vault runs on the shipped pack alone."""
    rel = "Knowledge Base/_Schema/project-keys.yaml"
    (stored / rel).unlink()
    assert not (stored / rel).exists()

    write.commit(writer, put(rel, b"projects:\n  harbour:\n    folder: Harbour\n"))
    assert "harbour" in project_keys.load_project_registry(stored).project_to_folder

    write.commit(writer, put(rel, b"projects:\n  quay:\n    folder: Quay\n"))
    keys = project_keys.load_project_registry(stored).project_to_folder
    assert "quay" in keys and "harbour" not in keys


def test_a_note_type_overlay_that_only_the_store_holds_reaches_the_registry_loader(
    stored, writer  # noqa: F811
):
    """The shared registry loader, which keys its cache on the overlay file: it serves the shipped
    pack for an overlay that the store holds and no file does, and keeps serving it after a
    commit."""
    from exomem import note_types

    rel = "Knowledge Base/_Schema/note-types.yaml"
    assert not (stored / rel).exists()
    pack = registry.load(note_types.SPEC, stored)
    document = (
        b"schema_version: 1\nentries:\n  harbour-log:\n    label: Harbour log\n"
        b"    description: A log of the harbour.\n"
        b"    attributes:\n      role: compiled\n      folder: Notes/Harbour\n"
    )

    write.commit(writer, put(rel, document))
    served = registry.load(note_types.SPEC, stored)

    assert "harbour-log" in served.entries
    assert "harbour-log" not in pack.entries
    assert served.content_hash != pack.content_hash
    assert vault_module.read_bytes_without_pinning(stored / rel, stored) == document


def test_the_terminal_ui_counts_the_stored_notes_and_starts_no_file_warm_up(
    stored, writer, monkeypatch  # noqa: F811
):
    """A session that starts the file warm-up in a store-mode vault builds its catalogue and drain
    queues from files that the vault does not have, and its home screen counts a note that only the
    store holds as none."""
    from exomem.tui.backend import ExomemBackend

    started = []
    monkeypatch.setattr(warmup, "start_background", lambda vault: started.append(vault))
    # The lean fallback sets process-wide environment variables, which this test must not leak.
    monkeypatch.setattr(ExomemBackend, "_apply_lean_fallback", staticmethod(lambda: None))
    backend = ExomemBackend(str(stored))
    assert backend.resolve_vault().initialized
    notes = backend.corpus_size()["notes"]

    write.commit(writer, put(NEW, EDITED))
    backend.start_runtime()

    assert not (stored / NEW).exists()
    assert backend.corpus_size() == {**backend.corpus_size(), "notes": notes + 1, "known": True}
    assert started == []
