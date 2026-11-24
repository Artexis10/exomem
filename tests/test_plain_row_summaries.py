"""A Records or Planning write reply names the user's things in plain words and never by vault path.

The workflow cases drive the MCP server over a temporary vault of each storage mode. The unit cases cover the
branches the workflow cannot reach: a row the caller may not read, and the closed set of restructure steps.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastmcp import Client
from lifecycle_fixtures import RECORDS_PATH, records_manifest, seed_vault
from record_fixtures import LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT
from test_collection_store_migration import env as store_env  # noqa: F401 - the fixture, by name
from test_name_pages_by_title import build as build  # noqa: F401 - the fixture, by name
from test_single_purpose_tools_mcp import _call

from exomem import commands, public_surface, record_governance, restructure, writer_lease
from exomem import server as server_module

_ROW = {"occurred_on": "2026-02-01", "title": "Panel shipment", "event_type": "dispatch"}
_LABEL = "2026-02-01, Panel shipment, dispatch"
_STRUCTURE_WORDS = re.compile(r"\b(records?|planning|collections?|schemas?|manifests?|entit(?:y|ies)|fields?)\b", re.I)


def _create(client: Client, path: str, manifest: str):
    return _call(client, "manage_memory", {"operation": "records.create", "arguments": {
        "manifest_path": path, "manifest_text": manifest, "why": "Track dispatch", "scaffold": True}})


def test_a_file_backed_row_append_names_the_row_and_its_collection_and_a_replay_says_the_same(build) -> None:
    async def run():
        async with Client(build()) as client:
            await _create(client, RECORDS_PATH, records_manifest(join=False))
            arguments = {"collection": RECORDS_PATH, "item": _ROW, "why": "Track dispatch"}
            saved = await _call(client, "add_record", arguments, key="row-1")
            replayed = await _call(client, "add_record", arguments, key="row-1")
            return saved, replayed

    saved, replayed = asyncio.run(run())

    assert saved["outcome"] == "committed"
    assert saved["user_summary"].startswith(f"Saved {_LABEL} in Delivery events (")
    assert saved["paths"][0] not in saved["user_summary"]
    assert replayed["user_summary"] == saved["user_summary"]


def test_a_store_backed_row_append_names_the_row_and_its_collection(store_env, capsys, monkeypatch) -> None:  # noqa: F811
    from conftest import _drain_background_threads
    from test_collection_store_migration import WORK, legacy_vault, migrate, printed

    root = legacy_vault(store_env)
    assert migrate(root, "prepare", operator=False) == 0
    printed(capsys)
    assert migrate(root, "apply") == 0
    printed(capsys)
    _drain_background_threads()
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")

    async def run():
        async with Client(server_module.build_server(require_auth=False)) as client:
            return await _call(client, "add_record", {
                "collection": WORK, "why": "Track dispatch", "item": {"title": "Panel shipment"},
            })

    try:
        saved = asyncio.run(run())
    finally:
        writer_lease.reset_managers_for_tests()

    assert saved["outcome"] == "committed"
    assert saved["user_summary"].startswith("Saved Panel shipment in Work (")
    assert saved["paths"][0] not in saved["user_summary"]
    assert saved["item_key"] and saved["after_item_hash"]


def test_a_row_the_caller_may_not_read_names_the_collection_alone(vault, monkeypatch) -> None:
    from lifecycle_fixtures import report_event

    from exomem import reply_names

    seed_vault(vault)
    leaf = report_event(vault, "Panel shipment", occurred_on="2026-02-01", event_type="dispatch")
    terminal = {"ok": True, "status": "committed", "leaf_result": leaf}
    arguments = {"item": _ROW}
    assert reply_names.resolve(vault, terminal, arguments) == reply_names.UserNames("Delivery events", _LABEL)

    released = record_governance.full_release_filter
    monkeypatch.setattr(
        record_governance, "full_release_filter",
        lambda root: lambda path: path not in leaf["affected_paths"] and released(root)(path),
    )

    assert reply_names.resolve(vault, terminal, arguments) == reply_names.UserNames("Delivery events", None)


def test_a_create_and_a_revise_name_the_collection_by_its_title_and_never_by_its_manifest_path(build) -> None:
    async def run():
        async with Client(build()) as client:
            created = await _create(client, LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT)
            inspected = await _call(client, "inspect_memory", {
                "operation": "records.inspect", "arguments": {"collection": LEDGER_COLLECTION_PATH}})
            revised = await _call(client, "manage_memory", {"operation": "records.revise", "arguments": {
                "collection": LEDGER_COLLECTION_PATH, "why": "Rename the book",
                "manifest_text": LEDGER_MANIFEST_TEXT.replace("Publications ledger", "Publications book"),
                **inspected["lifecycle_guards"]}})
            return created, revised

    created, revised = asyncio.run(run())

    assert created["user_summary"].startswith("Saved Publications ledger (")
    assert revised["user_summary"].startswith("Updated Publications book (")
    for reply in (created, revised):
        assert "_collection.md" not in reply["user_summary"] and "Records/" not in reply["user_summary"]


def test_a_planning_create_names_its_collection_alone(tmp_path) -> None:
    from conftest import initialize_vault_state_offline
    from test_planning_mutation import _manifest as planning_manifest

    from exomem import reply_names
    from exomem.plan_memory import plan_memory

    root = tmp_path / "planning-vault"
    (root / "Knowledge Base").mkdir(parents=True)
    (root / "Knowledge Base/log.md").write_text("# Existing log\n")
    initialize_vault_state_offline(root, source="plain planning create fixture")
    leaf = plan_memory(
        root, "create", manifest_path="Knowledge Base/Planning/Work/_collection.md",
        manifest_text=planning_manifest(), why="Track the work",
    )

    names = reply_names.resolve(root, {"ok": True, "status": "committed", "leaf_result": leaf}, {})

    assert names == reply_names.UserNames(names.collection)
    assert names.collection and "Planning/Work" not in names.collection


def test_a_restructure_says_what_the_collection_now_keeps_and_stays_silent_about_internals(build) -> None:
    async def restructure_call(client, **arguments):
        return await _call(client, "manage_memory", {"operation": "records.restructure", "arguments": {
            "collection": LEDGER_COLLECTION_PATH, "why": "Keep a note", **arguments}})

    async def run():
        async with Client(build()) as client:
            await _create(client, LEDGER_COLLECTION_PATH, LEDGER_MANIFEST_TEXT)
            added = await restructure_call(client, steps=[{"step": "add_field", "field": "editor_note", "type": "string"}])
            renamed = await restructure_call(client, steps=[{"step": "rename_field", "field": "editor_note", "to": "remark"}])
            return added, renamed

    added, renamed = asyncio.run(run())

    assert added["user_summary"].startswith("Publications ledger now keeps the editor note (")
    assert LEDGER_COLLECTION_PATH not in added["user_summary"]
    assert renamed["outcome"] == "committed"
    assert "user_summary" not in renamed


def test_every_step_kind_is_phrased_or_named_as_internal() -> None:
    assert {**restructure.STEP_PARAMETERS, **restructure._INVERSE_ONLY}.keys() == restructure.SUMMARY_PHRASES.keys() | restructure.INTERNAL_STEPS
    assert not set(restructure.SUMMARY_PHRASES) & restructure.INTERNAL_STEPS


def test_a_rekey_or_a_rename_or_a_retype_has_no_summary() -> None:
    steps = [
        {"step": "rekey", "key": ["slug"]},
        {"step": "rename_field", "field": "a", "to": "b"},
        {"step": "retype_field", "field": "b", "type": "integer"},
    ]

    assert restructure.summary(steps, "Quotes") is None


@pytest.mark.parametrize("step", [
    {"step": "add_field", "field": "price_eur", "type": "number"},
    {"step": "retire_field", "field": "price_eur"},
    {"step": "restore_field", "field": "price_eur"},
    {"step": "retitle", "title": "Estimates"},
    {"step": "split_out", "where": {"field": "closed", "equals": True}, "title": "Closed quotes"},
    {"step": "merge_into", "target": "Estimates", "mapping": {}},
    {"step": "retire"},
    {"step": "restore"},
])
def test_a_phrased_step_names_the_user_s_things_and_none_of_the_structure(step) -> None:
    said = restructure.summary([step], "Quotes", titled=lambda selector: selector)

    assert said
    assert not _STRUCTURE_WORDS.search(said)


def test_each_carrier_the_agent_reads_says_to_report_what_it_kept_in_the_users_words(vault) -> None:
    """The procedure, the bootstrap and the tool description are read at different moments, so each carries the line."""
    reference = Path(__file__).parents[1] / "src/exomem/_scaffold/_Schema/references/planning-records.md"
    procedure = " ".join(reference.read_text(encoding="utf-8").split())
    bootstrap = commands.op_bootstrap(vault, section="records_planning", profile="compact")["records"]["capture_examples"]
    tool = public_surface.public_surface().tools["add_record"].description

    assert "Report what you kept, in the user's words." in procedure
    assert "Report what you kept in the user's words." in bootstrap
    assert "Report what you kept in the user's words." in tool
    assert "Report the collection and the rows" not in procedure
