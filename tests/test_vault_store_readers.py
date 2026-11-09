"""What a store-mode vault's readers answer when the projected Markdown cannot be trusted.

OpenSpec make-vault-store-canonical, design D3, task 3.1 (first half). The vault is the migration
suite's generated vault, moved into store mode; pages that only the store holds are committed
through ``write.commit``, as the store thread commits them.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import (
    ALPHA,
    BETA,
    build_indexes,
    migrate_apply,
    vault,  # noqa: F401
)
from test_vault_store_read_write import EDITED, NEW, put, stored, writer  # noqa: F401

from exomem import __main__ as cli
from exomem import (
    commands,
    document_containers,
    get_page,
    lexstore,
    list_directory,
    project_keys,
    warmup,
)
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.collection_store import connection
from exomem.find_types import Hit
from exomem.governance import egress
from exomem.governance.principal import RequestPrincipal
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
    # The full recall tool: its default graph and hybrid ranking read each page via the page cache.
    asked = commands.op_ask_memory(root, query="lighthouse", limit=15)
    pages = [
        get_page.get_page(root, path=rel)
        for rel in (ALPHA, *(generated_page(index)[0] for index in generated))
    ]
    listing = list_directory.list_directory(root, path="Knowledge Base/Notes/Gen")
    notes = {
        limit: sorted(
            (
                entry.as_dict()
                for entry in list_directory.list_directory(
                    root, path="Knowledge Base/Notes", limit=limit
                ).entries
            ),
            key=lambda entry: entry["name"],
        )
        for limit in (None, 50)
    }
    return {
        "find": [(hit.path, hit.title, hit.excerpt) for hit in hits],
        "ask": [(hit["path"], hit.get("title"), hit.get("excerpt")) for hit in asked["hits"]],
        "notice": asked.get("notice"),
        "pages": [(page.content, page.content_hash) for page in pages],
        "listing": sorted(
            (entry.as_dict() for entry in listing.entries), key=lambda entry: entry["name"]
        ),
        "notes": notes,
        "corpus": sorted(corpus_pages(root)),
    }


#: The states a projected file takes when the store is the page: wrong bytes, none, or a link.
DAMAGE = ("poisoned", "deleted", "a link into the vault", "a link out of the vault")


def damage_every_owned_projection(root, damage):
    owned = [
        item.rel
        for item in ownership.iter_owned(root)
        if item.ownership is ownership.Ownership.PAGE
    ]
    inside = root / "Decoy.md"
    inside.write_bytes(SENTINEL)
    outside = root.parent / "outside.md"
    outside.write_bytes(SENTINEL)
    for rel in owned:
        path = root / rel
        if damage == "poisoned":
            path.write_bytes(SENTINEL)
            continue
        path.unlink()
        if damage != "deleted":
            path.symlink_to(inside if damage == "a link into the vault" else outside)


# Each case commits GENERATED pages and answers twice; a busy runner can exceed the 60 s default.
@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", DAMAGE)
def test_find_get_page_and_listing_answer_from_the_store_when_every_projected_file_is_damaged(
    stored, writer, damage, monkeypatch  # noqa: F811
):
    """A reader that still takes the file for the page: a stale projection, one that a failed
    projector left half-written, a missing one or a link at the page's name reaches the person who
    asks (or drops the page from them), and the store's page does not."""
    for index in range(GENERATED):
        rel, body = generated_page(index)
        write.commit(writer, put(rel, body))
    sampled = range(0, GENERATED, max(1, GENERATED // 10))
    find_module.clear_cache()
    scheduled = []
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: scheduled.append(1))
    before = answers(stored, sampled)
    stored_only = f"{generated_page(0)[0].rsplit('/', 1)[0]}/"
    for answered in (before["find"], before["ask"]):
        assert len(answered) == 15, "a full page of answers is what this check needs"
        assert any(hit[0].startswith(stored_only) for hit in answered), (
            "pages that only the store holds must be among the answers"
        )
    # Every lexical lane read the store corpus, so the answer is whole: the reader gets no notice,
    # and the owner's diagnostics name the routing.
    assert before["notice"] is None
    find_module.clear_cache()  # a cached answer would skip the routing that this reads
    diagnosed = commands.op_ask_memory(stored, query="lighthouse", limit=15, include_timings=True)
    assert not (diagnosed.get("warming") or {}).get("components")
    assert diagnosed["timings"]["profile"]["lexical_lanes"] == {
        "corpus": "store",
        "catalogue": "bypassed",
    }
    assert len(before["listing"]) == GENERATED
    assert len(before["corpus"]) >= GENERATED + 2
    for listed in before["notes"].values():
        assert {"Alpha.md", "Beta.md", "diagram.bin", "Gen"} <= {entry["name"] for entry in listed}

    damage_every_owned_projection(stored, damage)
    find_module.clear_cache()
    after = answers(stored, sampled)

    assert after == before
    assert SENTINEL.decode().strip() not in json.dumps(after)
    # That repair rebuilds the catalogue from files, so in store mode it would run on every find.
    assert scheduled == []


UNIT = "\n- [constraint] The lantern stays lit until dawn. ^lit\n"


def derived_row_answers(root):
    """What each catalogue reader of find answers, as plain data: keyword, browse, a category
    filter, the unit lane and the vault scope."""

    def found(**keywords):
        hits = find_module.find(
            root, limit=50, graph=False, rerank=False, mode="keyword", **keywords
        )
        return [(getattr(hit, "path", None) or hit.parent_path) for hit in hits]

    return {
        "keyword": found(query="lantern"),
        "keyword and type": found(query="lighthouse", types=["note"]),
        "browse": found(query="", types=["note"]),
        "category": found(query="", categories=["constraint"]),
        "units": found(query="lantern dawn", result_level="unit"),
        "mixed": found(query="lantern dawn", result_level="mixed"),
        "vault scope": found(query="lantern", scope="vault"),
    }


@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", DAMAGE)
def test_each_catalogue_reader_answers_from_the_store_as_it_answers_in_file_mode(
    vault, damage, monkeypatch  # noqa: F811
):
    """A reader that still takes the sidecar that files built, or the projected file, for the
    store's rows: it refuses, answers empty or serves the poison, where file mode answers the same
    bytes."""
    for index in range(GENERATED):
        rel, body = generated_page(index)
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_bytes(body + UNIT.encode() if index % 5 == 0 else body)
    build_indexes(vault)
    find_module.clear_cache()
    in_file_mode = derived_row_answers(vault)
    assert all(in_file_mode.values()), "each reader must have something to answer"
    assert migrate_apply(vault) == 0

    damage_every_owned_projection(vault, damage)
    sidecar = lexstore.lexical_path(vault)
    sidecar.unlink()
    sidecar.write_bytes(SENTINEL)  # a read of the file-built catalogue fails
    find_module.clear_cache()
    scheduled = []
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: scheduled.append(1))

    assert derived_row_answers(vault) == in_file_mode
    assert scheduled == []


@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", ("poisoned", "deleted"))
def test_a_store_mode_read_stats_no_stored_page_and_schedules_no_repair(
    stored, writer, damage, monkeypatch  # noqa: F811
):
    """A read path that still checks a stored page's file (a freshness witness, an mtime key, a
    first-use sync) pays a stat for every page on every request, and keys its answer on a file that
    the store does not hold."""
    for index in range(GENERATED):
        write.commit(writer, put(*generated_page(index)))
    # What file-mode reads of the pages left behind: a note remembered by its file's version.
    for path in stored.rglob("*.md"):
        version = document_containers._page_version(str(path))
        if version is not None and ownership.classify(path.relative_to(stored).as_posix()) is (
            ownership.Ownership.PAGE
        ):
            document_containers._store_unclaimed(
                (stored.resolve(), path.relative_to(stored).as_posix()),
                (str(path), version[0], version[1]),
            )
    damage_every_owned_projection(stored, damage)
    find_module.clear_cache()
    repairs, stats = [], []

    def watching(real):
        def watched(path, *arguments, **keywords):
            try:
                rel = Path(path).relative_to(stored).as_posix()
            except (TypeError, ValueError):
                rel = None  # a descriptor, or a path outside the vault
            if rel is not None and ownership.classify(rel) is ownership.Ownership.PAGE:
                stats.append(rel)
            return real(path, *arguments, **keywords)

        return watched

    monkeypatch.setattr(os, "stat", watching(os.stat))
    monkeypatch.setattr(os, "lstat", watching(os.lstat))
    monkeypatch.setattr(lexstore, "_schedule_repair", lambda *args, **kwargs: repairs.append(1))
    monkeypatch.setattr(lexstore, "request_repair", lambda *args, **kwargs: repairs.append(1))

    derived_row_answers(stored)
    find_module.find(stored, query="lighthouse", limit=15, graph=False, rerank=False)
    commands.op_ask_memory(stored, query="lighthouse", limit=15)

    assert stats == []
    assert repairs == []


@pytest.mark.parametrize("catalogue_off", ("the python backend", "a SQLite build without FTS5"))
def test_a_store_vault_reads_the_store_corpus_for_its_lexical_lanes_with_the_catalogue_off(
    stored, writer, catalogue_off, monkeypatch  # noqa: F811
):
    """A routing that waits for the file catalogue to be usable: with the catalogue off, the lexical
    lanes of a store-mode vault fall back to a build from files that never covers the store."""
    for index in range(GENERATED):
        write.commit(writer, put(*generated_page(index)))
    if catalogue_off == "the python backend":
        monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    else:
        monkeypatch.setattr(lexstore, "fts5_available", lambda: False)
    find_module.clear_cache()

    asked = commands.op_ask_memory(stored, query="lighthouse", limit=15, include_timings=True)

    assert any(hit["path"].startswith("Knowledge Base/Notes/Gen/") for hit in asked["hits"])
    assert asked["timings"]["profile"]["lexical_lanes"] == {
        "corpus": "store",
        "catalogue": "bypassed",
    }


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


def test_a_find_request_reads_one_store_snapshot_however_many_pages_it_reads(
    stored, writer, monkeypatch  # noqa: F811
):
    """A reader that opens a snapshot per page costs 7 to 14 ms a page and may mix revisions in one
    answer; the request's reads must not grow with the pages it touches."""
    opened = []
    real = connection.store_reader

    def counting(*arguments, **keywords):
        opened.append(1)
        return real(*arguments, **keywords)

    monkeypatch.setattr(connection, "store_reader", counting)

    def snapshots_for_one_find(pages):
        for index in range(pages):
            rel, body = generated_page(index)
            write.commit(writer, put(rel, body))
        find_module.clear_cache()
        opened.clear()
        hits = find_module.find(stored, query="lighthouse", limit=15, graph=False, rerank=False)
        assert len(hits) == 15
        return len(opened)

    few = snapshots_for_one_find(20)
    many = snapshots_for_one_find(60)

    assert many == few


def test_a_store_page_tagged_into_a_restricted_scope_is_withheld_whatever_its_projected_file_says(
    stored, writer  # noqa: F811
):
    """A decision read from the projected file releases a page that the store already holds in a
    restricted scope (the file still has the old tags, or none), and withholds an open page whose
    file has not been written yet."""
    governance = stored / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir()
    (governance / "scopes" / "secret.yaml").write_text(
        'governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\nname: Secret\ntags: ["secret"]\n'
    )
    (governance / "rules" / "secret-external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        'scope_ids: ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]\naudience: external\nceiling: 0\n'
    )
    restricted = "Knowledge Base/Notes/Gen/Restricted.md"
    open_page = "Knowledge Base/Notes/Gen/Open.md"
    write.commit(
        writer,
        write.StoreChange(
            write.Origin.GOVERNED,
            "test",
            puts=(
                write.PagePut(
                    restricted,
                    b"---\ntitle: R\ntype: note\ntags: [secret]\n---\n# R\n\nHeld back.\n",
                ),
                write.PagePut(open_page, b"---\ntitle: O\ntype: note\n---\n# O\n\nOpen.\n"),
            ),
        ),
    )
    # The restricted page's file is a stale projection from before the retag; the open page has
    # none.
    (stored / restricted).parent.mkdir(parents=True, exist_ok=True)
    (stored / restricted).write_bytes(b"---\ntitle: R\ntype: note\n---\n# R\n\nHeld back.\n")
    hits = [
        Hit(path=rel, type="note", scope="proj", title="t", updated="2026-01-01", excerpt="e",
            bm25_rank=1, vector_score=0.5)
        for rel in (restricted, open_page)
    ]

    released = egress.annotate_hits(
        stored, hits, principal=RequestPrincipal(audience_id="external", surface="mcp"), limit=5
    )

    assert [hit.path for hit in released.hits] == [open_page]


def test_a_decision_receipt_hashes_the_stored_page_not_its_projected_file(
    stored, writer  # noqa: F811
):
    """A receipt that hashes a lagging projection records bytes that no reader was served, and a
    store-only page loses its hash."""
    page = "Knowledge Base/Notes/Gen/Receipted.md"
    body = b"---\ntitle: Re\ntype: note\n---\n# Re\n\nStored bytes.\n"
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", puts=(write.PagePut(page, body),)),
    )

    assert egress._receipt_bytes(stored, page) == body  # no projected file yet
    (stored / page).parent.mkdir(parents=True, exist_ok=True)
    (stored / page).write_bytes(b"stale projection")
    assert egress._receipt_bytes(stored, page) == body


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
    monkeypatch.setattr(warmup, "start_background", lambda root: started.append(root))
    # The lean fallback sets process-wide environment variables, which this test must not leak.
    monkeypatch.setattr(ExomemBackend, "_apply_lean_fallback", staticmethod(lambda: None))
    backend = ExomemBackend(str(stored))
    assert backend.resolve_vault().initialized
    notes = backend.corpus_size()["notes"]

    write.commit(writer, put(NEW, EDITED))
    backend.start_runtime()

    assert not (stored / NEW).exists()
    after = backend.corpus_size()
    assert (after["notes"], after["known"]) == (notes + 1, True)
    assert started == []


def test_a_store_mode_listing_hides_only_a_disk_folder_that_holds_nothing_but_stray_pages(
    stored, writer  # noqa: F811
):
    """A listing that takes every folder on disk for a folder of the vault, or that hides a folder a
    person made: a stray Markdown file at a path that the store owns is no page, so a folder of
    only such files is not there. An empty folder, one that holds a binary and one that holds a
    stored page are."""
    notes = stored / "Knowledge Base/Notes"
    (notes / "Stray").mkdir()
    (notes / "Stray/stray.md").write_bytes(b"a file that the store never held\n")
    (notes / "Pictures").mkdir()
    (notes / "Pictures/photo.bin").write_bytes(b"\x00\x01")
    (notes / "Empty").mkdir()
    (notes / "Nested/Empty").mkdir(parents=True)
    write.commit(writer, put("Knowledge Base/Notes/Fresh/Page.md", EDITED))

    listing = list_directory.list_directory(stored, path="Knowledge Base/Notes", recursive=True)
    names = {entry.path.removeprefix("Knowledge Base/Notes/") for entry in listing.entries}

    assert {"Pictures", "Pictures/photo.bin", "Fresh", "Fresh/Page.md"} <= names
    assert {"Empty", "Nested", "Nested/Empty"} <= names
    assert not {name for name in names if name.startswith("Stray")}


def test_a_page_the_store_deleted_is_not_found_though_a_link_sits_at_its_name(
    stored, writer  # noqa: F811
):
    """A page read that follows a link at the deleted page's name returns the bytes of the page the
    link points to, as if the deleted page were still there."""
    write.commit(
        writer,
        write.StoreChange(write.Origin.GOVERNED, "test", deletes=(write.PageDelete(ALPHA),)),
    )
    (stored / ALPHA).unlink(missing_ok=True)
    (stored / ALPHA).symlink_to(stored / BETA)

    with pytest.raises(get_page.GetError) as missing:
        get_page.get_page(stored, path=ALPHA)

    assert missing.value.code == "NOT_FOUND"
    assert get_page.get_page(stored, path=BETA).content


def test_a_broken_state_root_leaves_a_file_vault_a_vault_and_a_bare_folder_not_one(
    stored  # noqa: F811
):
    """A vault probe that asks the store before the disk: a broken state root then fails every
    start of a vault whose sentinel is on disk, or raises for a directory that is no vault."""
    from exomem import state_migration, state_paths

    manifest = state_migration._manifest_path(state_paths.vault_state_dir(stored))
    manifest.write_bytes(b"not a manifest")

    assert vault_module._is_vault(stored)
    for sentinel in stored.rglob(vault_module._SCHEMA_SENTINEL):
        sentinel.unlink()
    assert not vault_module._is_vault(stored)
