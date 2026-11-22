"""Passage search fuses lexical matching with dense similarity, under the published-bytes rules."""
# The imported fixture is requested by the tests.
# ruff: noqa: F811

from __future__ import annotations

import contextlib
import os
import sqlite3

import numpy as np
import pytest
from test_passage_evidence import passage_tools, publish_reference  # noqa: F401

from exomem import commands, embedding_index, embeddings, schema, sidecar_store

QUERY = "lantern burn oil"


def save(call, title, passages, tags=()):
    saved = call(
        "save_source",
        {
            "title": title,
            "source_kind": "session",
            "tags": list(tags),
            "options": {"passages": passages},
        },
    )
    assert "error" not in saved, saved
    return saved


def search(call, limit=2, **options):
    request = {"query": QUERY, "limit": limit, "options": {"result_level": "passage", **options}}
    reply = call("search_memory", request)
    assert "error" not in reply, reply
    return reply


def keys(reply):
    return [hit["passage_key"] for hit in reply["hits"]]


def cosine(encoder, text):
    query, passage = encoder([QUERY, text])
    return float(np.dot(query, passage))


def test_a_passage_that_shares_only_word_forms_with_the_query_reaches_the_top_k(
    vault, passage_tools
):
    """The test encoder, like a weak model, scores only exact words, so it ranks the inflected
    passage last; its stems match every query term."""
    call, encoder = passage_tools
    save(call, "Harbour survey", [
        {"key": "repeats-one-word", "text": "Lantern lantern oil"},
        {"key": "inflected", "text": "The lanterns burned whale oil"},
        {"key": "mentions-one-word", "text": "lantern oil ledger notes"},
    ])
    embeddings.index_incremental(vault)
    publish_reference(vault)

    reply = search(call)

    assert keys(reply) == ["repeats-one-word", "inflected"]
    assert reply["ranking"] == "fused"
    # Each rank keeps the dense score of that rank, so a caller's score map still applies.
    assert [hit["score"] for hit in reply["hits"]] == pytest.approx([
        cosine(encoder, "Lantern lantern oil"), cosine(encoder, "lantern oil ledger notes"),
    ], abs=1e-4)


def test_a_lexical_match_is_answered_only_from_published_current_bytes(
    vault, passage_tools, monkeypatch
):
    """Lexical candidates come from stored chunk text, which can be stale against the Source or
    hold an edit that the corpus has not published."""
    # The capture starts a background graph rebuild that can unpublish the corpus after the edit.
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, encoder = passage_tools
    save(call, "Harbour survey", [
        {"key": "repeats-one-word", "text": "Lantern lantern oil"},
        {"key": "mentions-one-word", "text": "lantern oil ledger notes"},
    ])
    captured = {
        title: commands.op_capture_source(
            vault, schema.load_source_schema(vault), title=title, source_kind="session",
            passages=[{"key": title.lower(), "text": "The keeper sleeps."}],
        )["source"]["path"]
        for title in ("Stale", "Edited")
    }
    embeddings.index_incremental(vault)
    publish_reference(vault)
    # An editor changes the passage without a file event, and the index embeds the new bytes.
    edited = vault / captured["Edited"]
    edited.write_text(edited.read_text().replace("The keeper sleeps.", "Lanterns burned lamp oil"))
    # The index skips a file whose stored mtime is within a second of the file's.
    later = edited.stat().st_mtime + 5
    os.utime(edited, (later, later))
    embeddings.index_incremental(vault)
    # The stale row's vector ranks it third by dense similarity, so dense alone would answer
    # two passages; its text ranks it second lexically, after the edit.
    embeddings.get_embedding_index(vault).upsert_file(
        captured["Stale"], ["The lanterns burned oil"], encoder(["The lantern wick"]), 1.0
    )

    reply = search(call, limit=3)

    assert keys(reply) == ["repeats-one-word"]
    assert reply["withheld_chunks"] == 2
    assert "Withheld 2" in reply["user_summary"]


def test_vector_mode_ranks_by_exact_dense_similarity_alone(vault, passage_tools):
    """Dense-only ranking stays available for a caller that needs it unchanged."""
    call, encoder = passage_tools
    texts = {
        "repeats-one-word": "Lantern lantern oil",
        "inflected": "The lanterns burned whale oil",
        "mentions-one-word": "lantern oil ledger notes",
    }
    save(call, "Harbour survey", [{"key": key, "text": text} for key, text in texts.items()])
    embeddings.index_incremental(vault)
    publish_reference(vault)

    reply = search(call, mode="vector")

    assert keys(reply) == ["repeats-one-word", "mentions-one-word"]
    assert [hit["score"] for hit in reply["hits"]] == pytest.approx(
        [cosine(encoder, texts[key]) for key in keys(reply)], abs=1e-4
    )
    assert reply["ranking"] == "dense"
    assert search(call)["ranking"] == "fused"


def test_lexical_matches_outside_the_requested_tags_are_never_candidates(vault, passage_tools):
    """The lexical ranking reads chunk rows of every Source in the sidecar."""
    call, _encoder = passage_tools
    save(call, "Excluded log", [{"key": "excluded", "text": "Lanterns burned lamp oil"}],
         tags=["excluded"])
    save(call, "Harbour survey", [
        {"key": "repeats-one-word", "text": "Lantern lantern oil"},
        {"key": "inflected", "text": "The lanterns burned whale oil"},
        {"key": "mentions-one-word", "text": "lantern oil ledger notes"},
    ], tags=["readers"])
    embeddings.index_incremental(vault)
    publish_reference(vault)

    reply = call("search_memory", {
        "query": QUERY, "limit": 3, "tags": ["readers"], "options": {"result_level": "passage"},
    })

    assert "error" not in reply, reply
    assert keys(reply) == ["repeats-one-word", "inflected", "mentions-one-word"]
    assert reply["ranking"] == "fused"
    assert reply["withheld_chunks"] == 0



def test_a_chunk_rewritten_by_a_release_without_lexical_terms_is_matched_on_its_new_text(
    vault, passage_tools
):
    """After a rollback, an older release writes the sidecar without the terms triggers. SQLite
    can give the rewritten chunk the rowid of the row it replaced, whose terms remain; it
    always does when that row was the last one, which this case pins."""
    _call, encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Harbour log", source_kind="session",
        passages=[{"key": "log", "text": "The keeper sleeps."}],
    )
    path = saved["source"]["path"]
    embeddings.index_incremental(vault)
    index = embeddings.get_embedding_index(vault)
    assert index.select_passage_terms("keeper", 5, allowed_paths={path})
    with contextlib.closing(sqlite3.connect(index.path)) as older, older:
        [(rowid,)] = older.execute("SELECT rowid FROM chunks WHERE file_path = ?", (path,))
        older.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
        older.execute(
            "INSERT INTO chunks (rowid, file_path, chunk_idx, chunk_text, vector, file_mtime) "
            "VALUES (?, ?, 0, ?, ?, 0)",
            (rowid, path, "Whale oil", encoder(["Whale oil"])[0].tobytes()),
        )
        sidecar_store.bump_generation_for_paths(older, embedding_index.CHUNK_PATH_LOG, [path])

    assert [hit[:2] for hit in index.select_passage_terms("whale", 5, allowed_paths={path})] == [
        (path, 0)
    ]
    assert index.select_passage_terms("keeper", 5, allowed_paths={path}) == []
