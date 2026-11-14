"""Declared record passages survive capture and dense retrieval through the public tools."""

from __future__ import annotations

import asyncio
import hashlib

import numpy as np
import pytest
from fastmcp import Client
from test_vault_store_embeddings import install_encoder

from exomem import embeddings, memory_refs, readiness, server
from exomem import vault as vault_module


@pytest.fixture
def passage_tools(vault, monkeypatch):
    encoder = install_encoder(monkeypatch)
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    mcp = server.build_server(require_auth=False)

    def call(name, arguments):
        async def invoke():
            async with Client(mcp) as client:
                return await client.call_tool(name, arguments, raise_on_error=False)

        result = asyncio.run(invoke())
        payload = result.structured_content or {
            "error": " ".join(getattr(block, "text", "") for block in result.content)
        }
        return payload.get("result", payload)

    yield call, encoder
    readiness.reset()


def test_two_records_of_one_source_rank_with_verifiable_hashes_and_spans(vault, passage_tools):
    """Page-level collapse loses the second record even when both are the best matches."""
    call, encoder = passage_tools
    passages = [
        {"key": "r/42", "text": "The lantern lantern uses oil."},
        {"key": "r/43", "text": "The lantern keeper records oil deliveries."},
    ]
    identity = {"system": "archive", "record_id": "survey", "revision": "one"}
    captured = call(
        "save_source",
        {
            "title": "Harbour survey",
            "source_kind": "session",
            "detail": "full",
            "options": {"source_identity": identity, "passages": passages},
        },
    )
    assert captured.get("success", True), captured
    assert "error" not in captured, captured
    embeddings.index_incremental(vault)
    before = len(encoder.texts)
    reply = call(
        "search_memory",
        {
            "query": "lantern oil",
            "limit": 2,
            "options": {"result_level": "passage"},
        },
    )
    assert "error" not in reply, reply
    hits = reply["hits"]
    assert len(hits) == 2, reply
    assert len(encoder.texts) == before + 1
    assert {hit["passage_key"]: hit["passage_ordinal"] for hit in hits} == {"r/42": 1, "r/43": 2}
    assert hits[0]["score"] >= hits[1]["score"]
    assert hits[0]["ref"] == hits[1]["ref"]
    page = vault_module.read_guarded_text(
        vault, vault / memory_refs.resolve_identifier_read_only(vault, hits[0]["ref"])
    )[0]
    fields, body, _ = vault_module.parse_frontmatter(page)
    assert fields["passage_keys"] == ["r/42", "r/43"]
    for hit in hits:
        passage = passages[hit["passage_ordinal"] - 1]
        assert hit["source_identity"] == identity
        assert hit["passage_hash"] == hashlib.sha256(passage["text"].encode()).hexdigest()
        span = hit["span"]
        assert body[span["start"] : span["start"] + span["length"]] == passage["text"]
        assert "text" not in hit and "excerpt" not in hit
    assert reply["recall_space"]["dim"] == embeddings.VECTOR_DIM
    assert reply["withheld_chunks"] == 0


def test_long_passage_splits_into_adjacent_exact_spans(vault, passage_tools):
    """Whitespace normalization or an ordinal per chunk would break caller record verification."""
    call, _encoder = passage_tools
    text = " \t ".join(f"word{number}" for number in range(701))
    captured = call(
        "save_source",
        {
            "title": "Long record",
            "source_kind": "session",
            "options": {"passages": [{"key": "long/1", "text": text}]},
        },
    )
    assert "error" not in captured, captured
    embeddings.index_incremental(vault)
    reply = call(
        "search_memory", {"query": "word400", "limit": 10, "options": {"result_level": "passage"}}
    )
    assert "error" not in reply, reply
    hits = sorted(reply["hits"], key=lambda hit: hit["span"]["start"])
    assert len(hits) == 3
    assert {hit["passage_key"] for hit in hits} == {"long/1"}
    assert {hit["passage_ordinal"] for hit in hits} == {1}
    assert {hit["passage_hash"] for hit in hits} == {hashlib.sha256(text.encode()).hexdigest()}
    _fields, body, _marker = vault_module.parse_frontmatter(
        vault_module.read_guarded_text(
            vault, vault / memory_refs.resolve_identifier_read_only(vault, hits[0]["ref"])
        )[0]
    )
    parts = [
        body[hit["span"]["start"] : hit["span"]["start"] + hit["span"]["length"]] for hit in hits
    ]
    assert "".join(parts) == text
    assert [len(part.split()) for part in parts] == [350, 350, 1]
    for first, second in zip(hits[:-1], hits[1:], strict=True):
        assert first["span"]["start"] + first["span"]["length"] == second["span"]["start"]
    stored, _mtime = embeddings.get_embedding_index(vault).stored_chunks_for(
        memory_refs.resolve_identifier_read_only(vault, hits[0]["ref"])
    )
    assert [chunk.removeprefix("Long record\n\n") for chunk in stored] == parts


def test_keys_have_no_indexed_text_vector_or_score_signal(vault, passage_tools):
    """A key in the embedding input would make otherwise identical records rank differently."""
    call, _encoder = passage_tools
    for key in ("opaque:first", "opaque:second"):
        saved = call(
            "save_source",
            {
                "title": "Equal records",
                "source_kind": "session",
                "options": {
                    "passages": [{"key": key, "text": "The lantern burns oil every night."}]
                },
            },
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    reply = call(
        "search_memory",
        {"query": "lantern oil", "limit": 2, "options": {"result_level": "passage"}},
    )
    assert "error" not in reply, reply
    [first, second] = reply["hits"]
    assert {first["passage_key"], second["passage_key"]} == {"opaque:first", "opaque:second"}
    assert first["ref"] != second["ref"]
    assert first["score"] == second["score"]
    index = embeddings.get_embedding_index(vault)
    assert (
        index.stored_chunks_for(memory_refs.resolve_identifier_read_only(vault, first["ref"]))[0]
        == index.stored_chunks_for(memory_refs.resolve_identifier_read_only(vault, second["ref"]))[
            0
        ]
    )
    one, _ = index.stored_text_vectors(
        memory_refs.resolve_identifier_read_only(vault, first["ref"])
    )
    two, _ = index.stored_text_vectors(
        memory_refs.resolve_identifier_read_only(vault, second["ref"])
    )
    assert one.keys() == two.keys()
    for text, vector in one.items():
        assert np.array_equal(vector, two[text])
        assert "opaque:" not in text
    lexical = call("search_memory", {"query": "opaque:first", "options": {"mode": "keyword"}})
    assert lexical["hits"] == []


def test_stale_published_chunk_is_withheld_and_counted(vault, passage_tools):
    """A valid vector with stale stored text must not attest to the current record."""
    call, encoder = passage_tools
    saved = call(
        "save_source",
        {
            "title": "Stale binding",
            "source_kind": "session",
            "options": {"passages": [{"key": "current", "text": "The lantern burns oil."}]},
        },
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    before = call("search_memory", {"query": "lantern", "options": {"result_level": "passage"}})
    [hit] = before["hits"]
    index = embeddings.get_embedding_index(vault)
    index.upsert_file(
        memory_refs.resolve_identifier_read_only(vault, hit["ref"]),
        ["Stale binding\n\nThe lantern burned coal."],
        encoder(["Stale binding\n\nThe lantern burns oil."]),
        1.0,
    )
    after = call("search_memory", {"query": "lantern", "options": {"result_level": "passage"}})
    assert "error" not in after, after
    assert after["hits"] == []
    assert after["withheld_chunks"] == 1
    assert "Withheld 1" in after["user_summary"]


def test_tag_eligibility_precedes_selection_and_fills_limit(vault, passage_tools):
    """Filtering only the top chunks after ranking would leave fewer eligible matches."""
    call, _encoder = passage_tools
    for tag, text, key in (
        ("excluded", "lantern", "best"),
        ("readers", "lantern oil", "allowed-1"),
        ("readers", "lantern keeper", "allowed-2"),
    ):
        saved = call(
            "save_source",
            {
                "title": "Survey",
                "source_kind": "session",
                "tags": [tag],
                "options": {"passages": [{"key": key, "text": text}]},
            },
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    all_hits = call(
        "search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
    )
    assert all_hits["hits"][0]["passage_key"] == "best"
    reply = call(
        "search_memory",
        {
            "query": "lantern",
            "limit": 2,
            "tags": ["readers"],
            "options": {"result_level": "passage"},
        },
    )
    assert "error" not in reply, reply
    assert {hit["passage_key"] for hit in reply["hits"]} == {"allowed-1", "allowed-2"}


def test_identical_passage_retry_reuses_but_changed_key_conflicts(vault, passage_tools):
    """Hashing only the body would silently reuse a Source with the wrong caller key."""
    call, _encoder = passage_tools
    arguments = {
        "title": "Retry",
        "source_kind": "session",
        "detail": "full",
        "options": {
            "source_identity": {"system": "archive", "record_id": "retry", "revision": "one"},
            "passages": [{"key": "first", "text": "The lantern burns oil."}],
        },
    }
    saved = call("save_source", arguments)
    assert "error" not in saved, saved
    before = {path: path.read_bytes() for path in vault.rglob("*.md")}
    retry = call("save_source", arguments)
    assert retry["capture_outcome"] == "reused", retry
    changed = call(
        "save_source",
        {
            **arguments,
            "options": {
                **arguments["options"],
                "passages": [{"key": "second", "text": "The lantern burns oil."}],
            },
        },
    )
    assert "SOURCE_IDENTITY_CONFLICT" in str(changed), changed
    assert {path: path.read_bytes() for path in vault.rglob("*.md")} == before


@pytest.mark.parametrize(
    ("passages", "message"),
    [
        (
            [{"key": str(number), "text": "record"} for number in range(2001)],
            ("2001", "passages", "2000"),
        ),
        ([{"key": "", "text": "record"}], ("1", "key", "128")),
        ([{"key": 7, "text": "record"}], ("1", "key", "string")),
        ([{"key": "é" * 65, "text": "record"}], ("1", "key", "128")),
        (
            [{"key": "same", "text": "one"}, {"key": "same", "text": "two"}],
            ("2", "key", "passage 1"),
        ),
        ([{"key": "a", "text": ""}], ("1", "text", "nonempty")),
        ([{"key": "a", "text": "one\n\ntwo"}], ("1", "text", "blank line")),
        ([{"key": "a", "text": "one\n \t\ntwo"}], ("1", "text", "blank line")),
        ([{"key": "a", "text": "one\rtwo"}], ("1", "text", "carriage return")),
        ([{"key": "a", "text": " record"}], ("1", "text", "trim")),
        ([{"key": "a", "text": "record\t"}], ("1", "text", "trim")),
    ],
)
def test_each_declaration_bound_refuses_before_writing(vault, passage_tools, passages, message):
    """A declaration capture would normalize cannot produce a verifiable caller hash."""
    call, _encoder = passage_tools
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    refused = call(
        "save_source",
        {
            "title": "Invalid declaration",
            "source_kind": "session",
            "options": {"passages": passages},
        },
    )
    assert "error" in refused, refused
    for fragment in message:
        assert fragment in str(refused), refused
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("content", ["ordinary text", ""])
def test_passages_and_content_refuse_even_empty_content(vault, passage_tools, content):
    """Treating an empty explicit content as omitted would accept two original-input fields."""
    call, _encoder = passage_tools
    before = {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    refused = call(
        "save_source",
        {
            "title": "Two inputs",
            "source_kind": "session",
            "content": content,
            "options": {"passages": [{"key": "one", "text": "record"}]},
        },
    )
    assert "error" in refused, refused
    assert "content" in str(refused) and "passages" in str(refused)
    assert {path: path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize(
    "option", ["rerank", "graph", "prefer_compiled", "prefer_active", "prefer_used"]
)
def test_explicit_ranker_is_refused_before_encoding(passage_tools, option):
    """An ignored ranker would mislead the caller about the meaning of passage scores."""
    call, encoder = passage_tools
    before = len(encoder.texts)
    refused = call(
        "search_memory", {"query": "lantern", "options": {"result_level": "passage", option: True}}
    )
    assert "error" in refused, refused
    assert option in str(refused) and "remove" in str(refused), refused
    assert len(encoder.texts) == before


def test_passage_limit_above_200_refuses_before_encoding(passage_tools):
    """An unbounded passage selection would break the result-level contract."""
    call, encoder = passage_tools
    before = len(encoder.texts)
    refused = call(
        "search_memory", {"query": "lantern", "limit": 201, "options": {"result_level": "passage"}}
    )
    assert "error" in refused, refused
    assert "limit" in str(refused) and "200" in str(refused)
    assert len(encoder.texts) == before


def test_ordinary_source_chunks_remain_unchanged(vault, passage_tools):
    """Opting in one new Source must not change existing rows or trigger their reindex."""
    call, encoder = passage_tools
    saved = call(
        "save_source",
        {"title": "Ordinary", "source_kind": "session", "content": "A normal record."},
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    index = embeddings.get_embedding_index(vault)
    before = {
        path: (index.stored_chunks_for(path), index.stored_text_vectors(path)[0])
        for path in index.file_mtimes()
    }
    first = len(encoder.texts)
    declared = call(
        "save_source",
        {
            "title": "Declared",
            "source_kind": "session",
            "options": {"passages": [{"key": "new", "text": "The lantern burns oil."}]},
        },
    )
    assert "error" not in declared, declared
    for path, (chunks, vectors) in before.items():
        assert index.stored_chunks_for(path) == chunks
        current = index.stored_text_vectors(path)[0]
        assert vectors.keys() == current.keys()
        assert all(np.array_equal(vector, current[text]) for text, vector in vectors.items())
    assert all("Ordinary" not in text for text in encoder.texts[first:])


def test_maximum_declaration_keeps_every_128_byte_key(vault, passage_tools):
    """A frontmatter budget or YAML round trip must not truncate a legal declaration."""
    call, _encoder = passage_tools
    passages = [
        {"key": "é" * 62 + f"{number:04}", "text": f"record {number}"} for number in range(2000)
    ]
    saved = call(
        "save_source",
        {
            "title": "Maximum declaration",
            "source_kind": "session",
            "detail": "full",
            "options": {"passages": passages},
        },
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    reply = call(
        "search_memory",
        {"query": "record 1999", "limit": 200, "options": {"result_level": "passage"}},
    )
    assert "error" not in reply, reply
    assert len(reply["hits"]) == 200
    path = memory_refs.resolve_identifier_read_only(vault, reply["hits"][0]["ref"])
    fields, _body, _marker = vault_module.parse_frontmatter(
        vault_module.read_guarded_text(vault, vault / path)[0]
    )
    assert fields["passage_keys"] == [passage["key"] for passage in passages]
    assert all(len(hit["passage_key"].encode()) == 128 for hit in reply["hits"])
