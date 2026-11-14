"""Declared keys travel in canonical store bytes when Markdown projections are absent."""

# Imported fixtures are requested by each test's arguments.
# ruff: noqa: F811
from __future__ import annotations

import sys

import numpy as np
import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_embeddings import catch_up, encoder  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import stored, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, embeddings, find_corpus, schema
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
        assert b"declared_passages: 2" in page.body
    catch_up(served)
    reply = commands.op_ask_memory(served, query="lantern oil", result_level="passage", limit=2)
    assert {hit["passage_key"] for hit in reply["hits"]} == {"stored/7", "stored/8"}
    assert all(hit["path"] == path for hit in reply["hits"])
    assert all(hit["source_identity"]["record_id"] == "stored" for hit in reply["hits"])
    assert reply["withheld_chunks"] == 0


def test_keys_give_no_lexical_text_vector_or_score_signal_in_store_mode(served, encoder):
    """Store-derived lexical rows must strip declarations without a Markdown projection."""
    paths = []
    for key in ("opaque:first", "opaque:second"):
        saved = commands.op_capture_source(
            served,
            schema.load_source_schema(served),
            title="Equal records",
            source_kind="session",
            passages=[{"key": key, "text": "The lantern burns oil every night."}],
        )
        paths.append(saved["source"]["path"])
    assert all(not (served / path).exists() for path in paths)
    catch_up(served)
    pages = [find_corpus.CACHE.get(served / path, served) for path in paths]
    assert "opaque:first" in pages[0].body
    assert "opaque:second" in pages[1].body
    assert pages[0].search_body == pages[1].search_body
    assert "opaque:" not in pages[0].search_body
    index = embeddings.get_embedding_index(served)
    assert index.stored_chunks_for(paths[0])[0] == index.stored_chunks_for(paths[1])[0]
    one, _ = index.stored_text_vectors(paths[0])
    two, _ = index.stored_text_vectors(paths[1])
    assert one.keys() == two.keys()
    assert one
    for text, vector in one.items():
        assert np.array_equal(vector, two[text])
        assert "opaque:" not in text
    reply = commands.op_ask_memory(served, query="lantern oil", result_level="passage", limit=2)
    assert {hit["passage_key"] for hit in reply["hits"]} == {"opaque:first", "opaque:second"}
    assert reply["hits"][0]["score"] == reply["hits"][1]["score"]
    lexical = commands.op_ask_memory(served, query="opaque:first", mode="keyword")
    assert lexical["hits"] == []
