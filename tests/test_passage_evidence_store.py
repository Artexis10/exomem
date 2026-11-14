"""Declared keys travel in canonical store bytes when Markdown projections are absent."""

# Imported fixtures are requested by each test's arguments.
# ruff: noqa: F811
from __future__ import annotations

import sys

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_embeddings import catch_up, encoder  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, schema
from exomem.vault_store import read

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)


def test_passage_results_return_keys_from_store_without_a_projection(served, encoder):
    """A side file or a projection-only declaration would vanish in store mode."""
    saved = commands.op_capture_source(
        served,
        schema.load_source_schema(served),
        title="Stored records",
        source_kind="session",
        source_identity={"system": "archive", "record_id": "stored", "revision": "one"},
        passages=[
            {"key": "stored/7", "text": "The lantern burns oil."},
            {"key": "stored/8", "text": "The keeper orders oil."},
        ],
    )
    path = saved["source"]["path"]
    assert not (served / path).exists()
    with read.snapshot(served) as snapshot:
        page = snapshot.page(path)
        assert page is not None
        assert b"passage_keys" in page.body
    catch_up(served)
    reply = commands.op_ask_memory(served, query="lantern oil", result_level="passage", limit=2)
    assert {hit["passage_key"] for hit in reply["hits"]} == {"stored/7", "stored/8"}
    assert all(hit["path"] == path for hit in reply["hits"])
    assert all(hit["source_identity"]["record_id"] == "stored" for hit in reply["hits"])
    assert reply["withheld_chunks"] == 0
