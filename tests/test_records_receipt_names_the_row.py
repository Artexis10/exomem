"""A Records write receipt names the row and the collection a person recognises, never a rendered page.

The workflow test drives the real MCP server over a served summary collection: the incident class,
where a write touches the collection page and its rendered views. The unit cases cover what the
workflow cannot reach: the label a manifest declares, and a collection that declares none.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from types import SimpleNamespace

import pytest
from _public_call import public_call
from conftest import FIXTURE_VAULT
from fastmcp import Client
from test_collection_store_s1_gate import ROW
from test_collection_store_service import _store_thread_held_with_intake_on
from test_collection_store_service import service as service  # noqa: F401
from test_collection_store_writer import CID, manifest_path, manifest_text
from test_collection_store_writer import store as store  # noqa: F401

from exomem import command_surface, writer_lease
from exomem import server as server_module
from exomem.collection_store.preview import preview_store
from exomem.governance import egress
from exomem.governance.principal import owner_principal, request_scope


async def _tool(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(*public_call(name, arguments), raise_on_error=False)
    assert result.structured_content is not None
    return result.structured_content


@pytest.fixture
def served(service, monkeypatch):  # noqa: F811
    """The summary collection `Work`, served over MCP with the schema docs the server loads."""
    shutil.copytree(FIXTURE_VAULT / "Knowledge Base/_Schema", service.root / "Knowledge Base/_Schema",
                    dirs_exist_ok=True)
    monkeypatch.setattr(server_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    yield
    writer_lease.reset_managers_for_tests()


def test_add_and_edit_record_name_the_row_and_collection(served) -> None:
    async def run() -> None:
        async with Client(server_module.build_server(require_auth=False)) as client:
            added = await _tool(client, "add_record", {
                "collection": CID, "why": "Track dispatch", "item": {"title": "Panel shipment"},
            })
            assert added["outcome"] == "committed"
            assert added["user_summary"].startswith("Saved Panel shipment in Work (")
            rows = (await _tool(client, "query_records", {"collection": CID}))["rows"]
            seeded = next(row for row in rows if row["record_id"] == ROW)
            edited = await _tool(client, "edit_record", {
                "collection": CID, "why": "Count it", "record": seeded["id"], "changes": {"count": 3},
            })
            assert edited["outcome"] == "committed"
            assert edited["user_summary"].startswith("Updated Canonical in Work (")
            for reply in (added, edited):
                assert "_summary.md" not in reply["user_summary"]
                assert reply["item_key"] and reply["after_item_hash"]

    asyncio.run(run())


def test_a_replay_names_the_row_only_at_its_committed_version(served) -> None:
    async def run() -> None:
        async with Client(server_module.build_server(require_auth=False)) as client:
            call = public_call("add_record", {
                "collection": CID, "why": "Track dispatch", "item": {"title": "Panel shipment"},
            })
            meta = {command_surface.MCP_IDEMPOTENCY_META_KEY: "dispatch-shipment"}
            saved = (await client.call_tool(*call, meta=meta)).structured_content
            same = (await client.call_tool(*call, meta=meta)).structured_content
            assert same["user_summary"].startswith("Saved Panel shipment in Work (")
            rows = (await _tool(client, "query_records", {"collection": CID}))["rows"]
            row = next(row for row in rows if row["record_id"] == saved["item_key"])
            await _tool(client, "edit_record", {
                "collection": CID, "why": "Count it", "record": row["id"], "changes": {"count": 3},
            })
            moved_on = (await client.call_tool(*call, meta=meta)).structured_content
            assert moved_on["after_item_hash"] == saved["after_item_hash"]
            assert moved_on["user_summary"].startswith("Saved a row in Work (")

    asyncio.run(run())


def test_a_queued_add_record_names_its_row_when_the_applied_save_is_read_back(served, service, monkeypatch) -> None:
    """A save queued behind a busy store replays through the same release step, so its stored outcome names the row."""
    from exomem import write_intake

    async def run() -> dict:
        async with Client(server_module.build_server(require_auth=False)) as client:
            with _store_thread_held_with_intake_on(service, monkeypatch):
                queued = await _tool(client, "add_record", {
                    "collection": CID, "why": "Track dispatch", "item": {"title": "Panel shipment"},
                })
            assert queued["state"] == "queued"
            read_back: dict = {}
            deadline = time.monotonic() + 30
            while read_back.get("state") != "applied" and time.monotonic() < deadline:
                reply = await _tool(client, "inspect_memory", {
                    "operation": "review.queued_write", "arguments": {"ref": queued["queued_write"]["ref"]},
                })
                read_back = reply.get("queued_write", {})
                await asyncio.sleep(0.1)
            return read_back

    try:
        applied = asyncio.run(run())
    finally:
        write_intake.stop(service.root)
    assert applied.get("state") == "applied", applied
    summary = applied["outcome"]["user_summary"]
    assert summary.startswith("Saved Panel shipment in Work (")
    assert "_summary.md" not in summary


def test_a_collection_the_store_does_not_hold_keeps_its_path_summary(store) -> None:
    """A store-mode vault runs every write on the store thread, also one to a collection that only files hold."""
    store.create_collection(manifest_path(), manifest_text(), why="create")
    path = "Knowledge Base/Records/Loose/Items/9b208a0c-ea9d-50ba-afbb-0c537aae2207.md"
    receipt = {
        "user_summary": f"Saved -> {path} (personal Evermatter)", "paths": [path], "operation": "append",
        "collection_id": "6a1f0c52-3d84-4e0b-9b7e-2f5d8c1a9e47", "item_key": "9b208a0c-ea9d-50ba-afbb-0c537aae2207",
        "after_item_hash": "a" * 64, "outcome": "committed",
    }
    with preview_store(store.root, store.handle), request_scope(owner_principal()):
        named = egress.filter_withheld_entries(store.root, receipt, _item_receipt=True)
    assert named["user_summary"] == receipt["user_summary"]


def _manifest(*, natural_key: list[str], title_field: str | None = None, fields: tuple[str, ...] = ("name", "site")):
    presentation = None if title_field is None else SimpleNamespace(title=SimpleNamespace(field=title_field))
    return SimpleNamespace(
        item_presentation=presentation,
        schema=SimpleNamespace(
            natural_key=tuple(natural_key), fields={name: SimpleNamespace(type="string") for name in fields}),
    )


@pytest.mark.parametrize(("manifest", "values", "label"), [
    (_manifest(natural_key=["name"], title_field="site"), {"name": "N-1", "site": "North yard"}, "North yard"),
    (_manifest(natural_key=["name", "site"]), {"name": "N-1", "site": "North yard"}, "N-1, North yard"),
    (_manifest(natural_key=["name"]), {"site": "North yard"}, None),
    (_manifest(natural_key=["name"]), {"name": "a  long\nname " + "x" * 200}, "a long name " + "x" * 67 + "…"),
])
def test_row_label_comes_from_what_the_collection_declares(manifest, values, label) -> None:
    from exomem.collection_store import receipt_names

    assert receipt_names.row_label(manifest, values) == label
