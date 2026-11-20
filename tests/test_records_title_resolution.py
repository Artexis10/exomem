"""A Records collection is named by its title, and a row's record id says what it is.

Live failures, both preserved here: a served `query_records` by the title that the inventory shows
refused `INVALID_COLLECTION_PATH` while the same collection's path answered, and `edit_record`
by a row's `record_id` refused `ITEM_NOT_FOUND` for a row that exists. The served cases run the
dispatcher that MCP and REST share, on a migrated vault, because the store thread is where a
title could not be seen.
"""

from __future__ import annotations

import contextvars
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT, ledger_item
from test_collection_field_admission import GUEST
from test_collection_store_migration import (  # noqa: F401
    MIGRATE,
    X3,
    env,
    legacy_vault,
    migrate,
    start_service,
)
from test_collection_store_s1_gate import _until
from test_collection_store_service import ON, Service, _coordinator_unreachable, call
from test_governance_egress import write_rule, write_scope

from exomem import commands, writer_lease
from exomem import structured_collections as collections
from exomem.cli_ops import OpError
from exomem.collection_store import capability, runtime
from exomem.collection_store.connection import CollectionStoreError
from exomem.governance.principal import request_scope
from exomem.record_memory import record_memory

DATASET = "Knowledge Base/Records/dataset/_collection.md"
STORE_TITLE = "X3 training sessions"  # held by a store collection after the migration
FILE_TITLE = "Meter readings"  # held by the dataset, which stays in files


@pytest.fixture
def migrated(env, monkeypatch):  # noqa: F811
    """A migrated vault that no service serves yet, and its root: a store collection and a file one."""
    root = legacy_vault(env)
    assert migrate(root, "prepare", operator=False) == 0 and migrate(root, "apply") == 0
    monkeypatch.setattr(capability, "RELEASED", MIGRATE | ON)
    return root


@pytest.fixture
def served(migrated):
    """The migrated vault under a running service that serves its store."""
    service = start_service(migrated)
    yield migrated
    service.stop()


def _answer(root, collection):
    """What a served query by this reference answers: its rows, or the refusal that the caller reads."""
    try:
        reply = call(root, "record_memory", action="query", collection=collection)
    except OpError as error:
        return error.as_public_dict()
    return reply["collection_id"], reply["rows"]


@pytest.mark.parametrize(("title", "path"), [
    (STORE_TITLE, X3),
    (f"  {STORE_TITLE.upper()} ", X3),
    (FILE_TITLE, DATASET),
], ids=["store title", "store title, other case and spaces", "file title"])
def test_a_served_query_by_title_answers_what_the_manifest_path_answers(served, title, path) -> None:
    by_path = _answer(served, path)

    assert by_path[1], "the collection must hold rows, or the comparison proves nothing"
    assert _answer(served, title) == by_path


def _retitle_dataset(root, title):
    manifest = root / DATASET
    manifest.write_text(manifest.read_text().replace(f"title: {FILE_TITLE}", f"title: {title}"))


def test_a_served_title_held_by_a_store_and_a_file_collection_names_both_paths(served) -> None:
    _retitle_dataset(served, STORE_TITLE.upper())

    refusal = _answer(served, STORE_TITLE)

    assert refusal["code"] == "AMBIGUOUS_COLLECTION"
    assert refusal["candidates"] == sorted([X3, DATASET])


def test_a_served_title_counts_only_the_collections_the_caller_may_see(served) -> None:
    """The dataset shares the store collection's title but is withheld: the title is not ambiguous."""
    _retitle_dataset(served, STORE_TITLE.upper())
    (served / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/dataset\n")

    assert _answer(served, STORE_TITLE) == _answer(served, X3)


def test_a_served_title_held_only_by_a_withheld_collection_answers_as_a_title_nobody_holds(served) -> None:
    (served / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/Health/X3\n")

    assert _answer(served, STORE_TITLE) == _answer(served, "Evening sauna log")


def test_a_served_title_answers_the_same_whether_its_file_collection_is_withheld_or_deleted(served) -> None:
    (served / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/dataset\n")
    withheld = _answer(served, FILE_TITLE)
    (served / "Knowledge Base/_access.yaml").unlink()
    shutil.rmtree(served / Path(DATASET).parent)

    assert _answer(served, FILE_TITLE) == withheld


# While the store opens, or cannot open, the store collections' titles are unknown, not absent.


def test_a_title_while_the_store_cannot_open_gets_the_refusal_its_path_gets(migrated, monkeypatch) -> None:
    monkeypatch.setattr(writer_lease.LeaseCoordinatorClient, "collection_store_fence", _coordinator_unreachable)
    service = Service(migrated, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime._server(migrated.resolve()).refusal[0] == "WRITER_COORDINATOR_UNAVAILABLE")
        by_path = _answer(migrated, X3)

        assert by_path["code"] == "WRITER_COORDINATOR_UNAVAILABLE"
        assert _answer(migrated, STORE_TITLE) == by_path
    finally:
        service.stop()


def test_a_title_while_the_store_opens_waits_for_the_answer_its_path_waits_for(migrated, monkeypatch) -> None:
    opening, opened = threading.Event(), threading.Event()
    open_store = runtime.StoreServer._open

    def slow_open(server):
        opening.set()
        assert opened.wait(30), "the test did not let the store open"
        open_store(server)

    monkeypatch.setattr(runtime.StoreServer, "_open", slow_open)
    service = Service(migrated, writer_lease.start_server_lifecycle())
    try:
        assert opening.wait(10)
        server = runtime._server(migrated.resolve())
        with ThreadPoolExecutor(2) as pool:
            by_path = pool.submit(contextvars.copy_context().run, _answer, migrated, X3)
            by_title = pool.submit(contextvars.copy_context().run, _answer, migrated, STORE_TITLE)
            _until(lambda: len(server._requests) == 2 or by_title.done())
            assert not by_title.done(), by_title.result()  # it waits for the store, as the path does
            opened.set()

            assert by_path.result()[1], "the collection must hold rows, or the comparison proves nothing"
            assert by_title.result() == by_path.result()
    finally:
        opened.set()
        service.stop()


def _guest_answer(root, collection):
    """What a caller that may see no Records collection hears for a query by this reference."""
    command = next(command for command in commands.product_commands_for("mcp") if command.name == "record_memory")
    with request_scope(GUEST):
        try:
            return writer_lease.invoke_command(command, root, action="query", collection=collection)
        except OpError as error:
            return error.as_public_dict()
        except (collections.CollectionError, CollectionStoreError) as error:
            return error.code, str(error)


@pytest.mark.parametrize("store", ["cannot open", "opening"])
def test_store_collections_withheld_from_the_caller_leave_its_title_answer_unchanged_while_the_store_opens(
        migrated, monkeypatch, store) -> None:
    """A caller that may see no store collection learns nothing from the store's state: a store
    collection's title answers as it does where no service runs."""
    write_scope(migrated, paths="Records/**")
    write_rule(migrated, ceiling=0)
    without_service = _guest_answer(migrated, STORE_TITLE)
    opened = threading.Event()
    if store == "cannot open":
        monkeypatch.setattr(writer_lease.LeaseCoordinatorClient, "collection_store_fence", _coordinator_unreachable)
    else:
        open_store = runtime.StoreServer._open
        monkeypatch.setattr(runtime.StoreServer, "_open", lambda server: (opened.wait(30), open_store(server))[1])
    service = Service(migrated, writer_lease.start_server_lifecycle())
    try:
        _until(lambda: runtime.served(migrated))
        if store == "cannot open":
            _until(lambda: runtime._server(migrated.resolve()).refusal[0] == "WRITER_COORDINATOR_UNAVAILABLE")

        assert _guest_answer(migrated, STORE_TITLE) == without_service
    finally:
        opened.set()
        service.stop()


# A file vault, where the facade resolves on the calling thread.

LEDGER_TITLE = "Publications ledger"
SECOND_LEDGER = "Knowledge Base/Records/Archive/_collection.md"


def _ledger(vault, manifest_path=LEDGER_COLLECTION_PATH, *, title=LEDGER_TITLE, exomem_id=None):
    text = LEDGER_MANIFEST_TEXT.replace(f"title: {LEDGER_TITLE}", f"title: {title}")
    if exomem_id:
        text = text.replace("5e9d1e2f-8f0a-4b6c-9d21-6f0f4a2b7c31", exomem_id)
    (vault / "Knowledge Base").mkdir(parents=True, exist_ok=True)
    (vault / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
    record_memory(vault, "create", manifest_path=manifest_path, manifest_text=text, scaffold=True, why="seed")


def _refusal(vault, collection):
    with pytest.raises(OpError) as raised:
        record_memory(vault, "query", collection=collection)
    return raised.value.as_public_dict()


def test_a_title_resolves_to_the_file_collection_it_names(tmp_path) -> None:
    _ledger(tmp_path)
    record_memory(tmp_path, "append", collection=LEDGER_COLLECTION_PATH, item=ledger_item(), why="log it")

    by_title = record_memory(tmp_path, "query", collection=f" {LEDGER_TITLE.lower()} ")

    assert [row["slug"] for row in by_title["rows"]] == ["quarterly-note"]
    assert by_title["rows"] == record_memory(tmp_path, "query", collection=LEDGER_COLLECTION_PATH)["rows"]


def test_a_title_answers_the_same_whether_its_file_collection_is_withheld_or_deleted(tmp_path) -> None:
    withheld, deleted = tmp_path / "withheld", tmp_path / "deleted"
    for vault in (withheld, deleted):
        _ledger(vault)
        (vault / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/Publications\n")
    shutil.rmtree(deleted / Path(LEDGER_COLLECTION_PATH).parent)

    assert _refusal(withheld, LEDGER_TITLE) == _refusal(deleted, LEDGER_TITLE)


def test_a_file_title_counts_only_the_collections_the_caller_may_see(tmp_path) -> None:
    _ledger(tmp_path)
    record_memory(tmp_path, "append", collection=LEDGER_COLLECTION_PATH, item=ledger_item(), why="log it")
    _ledger(tmp_path, SECOND_LEDGER, title=LEDGER_TITLE.upper(), exomem_id="0b6c7d2e-4f1a-4d3b-8e55-1a2b3c4d5e6f")
    (tmp_path / "Knowledge Base/_access.yaml").write_text("excluded:\n  - Records/Archive\n")

    by_title = record_memory(tmp_path, "query", collection=LEDGER_TITLE)

    assert [row["slug"] for row in by_title["rows"]] == ["quarterly-note"]


# A row's record id names the row but no version of it, so it guards no edit.


def test_a_record_id_of_a_file_row_the_caller_reads_is_refused_as_a_record_id_not_as_a_missing_row(tmp_path) -> None:
    _ledger(tmp_path)
    record_memory(tmp_path, "append", collection=LEDGER_COLLECTION_PATH, item=ledger_item(), why="log it")
    row = record_memory(tmp_path, "query", collection=LEDGER_COLLECTION_PATH)["rows"][0]

    with pytest.raises(OpError) as raised:
        record_memory(tmp_path, "update", collection=LEDGER_COLLECTION_PATH, item_id=row["record_id"],
                      changes={"word_count": 50}, why="correct")

    assert raised.value.code == "ITEM_ID_IS_KEY"
    assert "record_id" in str(raised.value) and "`id`" in str(raised.value)
    record_memory(tmp_path, "update", collection=LEDGER_COLLECTION_PATH, item_id=row["id"],
                  changes={"word_count": 50}, why="correct")  # the version-bound id still edits
