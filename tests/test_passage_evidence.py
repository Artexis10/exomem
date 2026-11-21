"""Declared record passages survive capture and dense retrieval through the public tools."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import anyio
import numpy as np
import pytest
from fastmcp import Client
from test_vault_store_embeddings import install_encoder

from exomem import (
    cli_ops,
    commands,
    embeddings,
    find,
    find_corpus,
    freshness,
    lexstore,
    memory_refs,
    page_names,
    passage_evidence,
    readiness,
    reserved_paths,
    runtime_resources,
    schema,
    semantic_contract,
    server,
    server_runtime,
)
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope

# `install_encoder` replaces the encoder seam; a test of which model turn the query takes runs this one.
REAL_EMBED_TEXTS = embeddings.embed_texts


def bytes_kept_alive(holder, *owned_elsewhere):
    """The size of every object `holder` reaches, once each, leaving out `owned_elsewhere`."""
    seen, pending, kept = {id(item) for item in owned_elsewhere}, [holder], 0
    while pending:
        item = pending.pop()
        if id(item) in seen or isinstance(item, type):
            continue
        seen.add(id(item))
        kept += sys.getsizeof(item)
        pending.extend(gc.get_referents(item))
    return kept


def publish_reference(vault):
    """Prepare the maintained corpus and catalog before a service request."""
    # These are the two fixed freshness scopes of the search protocol.
    for scope in ("kb", "vault"):
        freshness.seed(
            vault, scope,
            [(str(path), freshness.stat_signature(path)) for path in find._corpus_walk(vault, scope)],
        )
    assert lexstore.get_store(vault).rebuild_atomic()
    semantic_contract.warm_corpus_context(vault)


@pytest.fixture
def passage_tools(vault, monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    # CI's core tier sets the legacy alias; the server would promote it back to EXOMEM_DISABLE_EMBEDDINGS.
    monkeypatch.delenv("KB_MCP_DISABLE_EMBEDDINGS", raising=False)
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


@pytest.mark.parametrize("scoring_mode", ["disk", "resident"])
def test_two_records_of_one_source_rank_with_verifiable_hashes_and_spans(
    vault, passage_tools, monkeypatch, scoring_mode
):
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
    publish_reference(vault)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    monkeypatch.setenv("EXOMEM_PASSAGE_MATRIX_BYTES", str(1024 * 1024) if scoring_mode == "resident" else "0")
    monkeypatch.delenv("EXOMEM_DISABLE_WARMUP", raising=False)
    server_runtime._warm_embedding_matrix(vault)
    if scoring_mode == "resident":
        assert embeddings.get_embedding_index(vault)._cache is not None
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
    assert fields["declared_passages"] == 2
    assert "passage_keys" not in fields
    for hit in hits:
        passage = passages[hit["passage_ordinal"] - 1]
        assert hit["source_identity"] == identity
        assert hit["passage_hash"] == hashlib.sha256(passage["text"].encode()).hexdigest()
        span = hit["span"]
        assert body[span["start"] : span["start"] + span["length"]] == passage["text"]
        assert "text" not in hit and "excerpt" not in hit
    assert reply["recall_space"]["dim"] == embeddings.VECTOR_DIM
    assert reply["withheld_chunks"] == 0
    assert reply["scoring_mode"] == scoring_mode
    embeddings.get_embedding_index(vault).unload_cache()


def test_four_admitted_public_requests_all_complete(vault, passage_tools, monkeypatch):
    """Reader contention must not look like a busy model or unavailable publication."""
    call, encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Concurrent records", source_kind="session",
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    start = threading.Barrier(4)
    publishing, release = threading.Event(), threading.Event()

    def unrelated_publisher():
        with semantic_contract._CORPUS_CONTEXT_UPDATE_LOCK:
            publishing.set()
            assert release.wait(10)

    def slow_encode(texts, *, is_query=False):
        # Stand in for a nonzero model turn; this is admission proof, not a latency measurement.
        threading.Event().wait(0.02)
        return encoder(texts, is_query=is_query)

    monkeypatch.setattr(embeddings, "embed_texts", slow_encode)

    def search(_number):
        start.wait(timeout=10)
        return call(
            "search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        publisher = pool.submit(unrelated_publisher)
        assert publishing.wait(5)
        try:
            replies = list(pool.map(search, range(4)))
        finally:
            release.set()
        publisher.result()
    assert all("error" not in reply and len(reply.get("hits", [])) == 1 for reply in replies), replies


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
    publish_reference(vault)
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
    assert stored == parts


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
    publish_reference(vault)
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
    pages = [
        find_corpus.CACHE.get(
            vault / memory_refs.resolve_identifier_read_only(vault, hit["ref"]), vault
        )
        for hit in (first, second)
    ]
    assert pages[0].search_body == pages[1].search_body
    assert one.keys() == two.keys()
    for text, vector in one.items():
        assert np.array_equal(vector, two[text])
        assert "opaque:" not in text
    lexical = call("search_memory", {"query": "opaque:first", "options": {"mode": "keyword"}})
    assert lexical["hits"] == []


def test_declared_passages_embed_exactly_their_text_whatever_the_title(vault, passage_tools):
    """A title in the embedding input would make a caller's scores depend on what it names the Source."""
    call, _encoder = passage_tools
    text = "The lantern burns oil every night."
    for title in ("Harbour log", "Survey of the northern lights"):
        saved = call(
            "save_source",
            {
                "title": title,
                "source_kind": "session",
                "options": {"passages": [{"key": "record/1", "text": text}]},
            },
        )
        assert "error" not in saved, saved
    ordinary = call(
        "save_source", {"title": "Agent note", "source_kind": "session", "content": text}
    )
    assert "error" not in ordinary, ordinary
    embeddings.index_incremental(vault)
    publish_reference(vault)
    reply = call(
        "search_memory",
        {"query": "lantern oil", "limit": 2, "options": {"result_level": "passage"}},
    )
    assert "error" not in reply, reply
    [first, second] = reply["hits"]
    assert first["ref"] != second["ref"]
    assert first["score"] == second["score"]
    index = embeddings.get_embedding_index(vault)
    vectors = []
    for hit in (first, second):
        path = memory_refs.resolve_identifier_read_only(vault, hit["ref"])
        assert index.stored_chunks_for(path)[0] == [text]
        vectors.append(index.stored_text_vectors(path)[0][text])
    assert np.array_equal(vectors[0], vectors[1])
    # A Source saved without declared passages keeps its title in the embedded text.
    [ordinary_page] = (vault / "Knowledge Base/Sources").rglob("*agent-note.md")
    ordinary_path = ordinary_page.relative_to(vault).as_posix()
    assert index.stored_chunks_for(ordinary_path)[0][0].startswith("Agent note\n\n")


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
    publish_reference(vault)
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


def test_tag_eligibility_precedes_selection_and_fills_limit(vault, passage_tools, monkeypatch):
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
    publish_reference(vault)
    all_hits = call(
        "search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
    )
    assert all_hits["hits"][0]["passage_key"] == "best"
    reads = []
    original = reserved_paths.read_generic_bytes

    def counted(root, path, **kwargs):
        if "Sources/" in str(path):
            reads.append(str(path))
        return original(root, path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", counted)
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
    assert len(set(reads)) == 2, reads
    assert set(reads) == {
        memory_refs.resolve_identifier_read_only(vault, hit["ref"]) for hit in reply["hits"]
    }


def test_a_tag_change_reaches_the_next_tagged_passage_search(vault, passage_tools):
    """A tagged search answered before the edit must not keep a Source the edit untagged."""
    call, _encoder = passage_tools
    for key in ("kept", "retagged"):
        saved = call(
            "save_source",
            {
                "title": f"Survey {key}",
                "source_kind": "session",
                "tags": ["readers"],
                "options": {"passages": [{"key": key, "text": f"lantern {key}"}]},
            },
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    request = {
        "query": "lantern", "limit": 2, "tags": ["readers"], "options": {"result_level": "passage"}
    }
    before = call("search_memory", request)
    assert {hit["passage_key"] for hit in before["hits"]} == {"kept", "retagged"}, before
    retagged = memory_refs.resolve_identifier_read_only(
        vault, next(hit["ref"] for hit in before["hits"] if hit["passage_key"] == "retagged")
    )

    # Sources are append-only for agents, so the owner retags the file in an editor.
    fields, body, _ = vault_module.parse_frontmatter((vault / retagged).read_text())
    fields["tags"] = ["writers"]
    (vault / retagged).write_text(
        "---\n" + vault_module.serialize_frontmatter(fields) + "\n---\n\n" + body
    )
    embeddings.index_incremental(vault)
    publish_reference(vault)

    after = call("search_memory", request)
    assert "error" not in after, after
    assert [hit["passage_key"] for hit in after["hits"]] == ["kept"], after


def test_a_second_tag_filter_gets_its_own_passage_answer(vault, passage_tools):
    """The eligible set of one filter must never answer another filter at the same corpus state."""
    call, _encoder = passage_tools
    for tag in ("readers", "writers"):
        saved = call(
            "save_source",
            {
                "title": f"Survey for {tag}",
                "source_kind": "session",
                "tags": [tag],
                "options": {"passages": [{"key": tag, "text": f"lantern for {tag}"}]},
            },
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    for tag in ("readers", "writers"):
        reply = call(
            "search_memory",
            {"query": "lantern", "limit": 2, "tags": [tag], "options": {"result_level": "passage"}},
        )
        assert "error" not in reply, reply
        assert [hit["passage_key"] for hit in reply["hits"]] == [tag], reply


def test_a_restricted_caller_after_an_owner_search_sees_only_its_admitted_passages(
    vault, passage_tools
):
    """A set answered for the owner must not stand in for a narrower caller's admission."""
    from exomem.governance.principal import owner_principal, request_scope

    call, _encoder = passage_tools
    for key in ("shared", "withheld"):
        saved = call(
            "save_source",
            {
                "title": f"Survey {key}",
                "source_kind": "session",
                "tags": ["readers"],
                "options": {"passages": [{"key": key, "text": f"lantern {key}"}]},
            },
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    request = {"query": "lantern", "limit": 2, "tags": ["readers"], "result_level": "passage"}
    with request_scope(owner_principal()):
        owner = find.find(vault, **request)
        assert {hit["passage_key"] for hit in owner["hits"]} == {"shared", "withheld"}, owner
        withheld = memory_refs.resolve_identifier_read_only(
            vault, next(hit["ref"] for hit in owner["hits"] if hit["passage_key"] == "withheld")
        )
        restricted = find.find(vault, **request, admit_path=lambda path: path != withheld)
    assert [hit["passage_key"] for hit in restricted["hits"]] == ["shared"], restricted


def test_an_eligible_set_read_before_an_erasure_cutoff_is_not_served_after_it(vault, monkeypatch):
    """An erasure removes catalogue rows before it advances the cutoff, so the corpus checkpoint
    alone cannot prove that a set read earlier still holds."""
    from exomem import source_custody, structured_filters, writer_lease

    publish_reference(vault)
    current = {"paths": {"Sources/erased.md", "Sources/kept.md"}}
    monkeypatch.setattr(
        find, "_managed_eligible_filter_paths", lambda *args, **kwargs: set(current["paths"])
    )
    plan = structured_filters.compile_filter(
        None, shortcuts=structured_filters.FilterShortcuts(tags=("readers",))
    )

    def eligible():
        snapshot = find.FreshnessSnapshot(vault, require_live_recall=True)
        return find._reusable_eligible_filter_paths(
            vault, scope="vault", plan=plan, snapshot=snapshot
        )

    assert eligible() == {"Sources/erased.md", "Sources/kept.md"}
    current["paths"] = {"Sources/kept.md"}
    with writer_lease.active_manager().mutation_guard(vault):
        assert source_custody.advance_retirement_generation(vault)
    assert eligible() == {"Sources/kept.md"}


def test_a_unit_filter_is_resolved_for_every_passage_search(vault, monkeypatch):
    """A unit filter's matches follow the selected interpretation, which no part of the cache's
    proof names, so a reused set could keep a unit that no longer matches."""
    from exomem import structured_filters

    publish_reference(vault)
    current = {"paths": {"Sources/first.md"}}
    monkeypatch.setattr(
        find, "_managed_eligible_filter_paths", lambda *args, **kwargs: set(current["paths"])
    )
    plan = structured_filters.compile_filter({"unit.tags": {"$contains": "readers"}})

    def eligible():
        snapshot = find.FreshnessSnapshot(vault, require_live_recall=True)
        return find._reusable_eligible_filter_paths(
            vault, scope="vault", plan=plan, snapshot=snapshot
        )

    assert eligible() == {"Sources/first.md"}
    current["paths"] = {"Sources/second.md"}
    assert eligible() == {"Sources/second.md"}


@pytest.mark.parametrize("anchor_state", ["visible", "tombstoned", "revoked"])
def test_relation_scope_uses_its_index_without_reading_the_source_anchor(
    vault, passage_tools, monkeypatch, anchor_state
):
    """An unselected Source anchor must not add a canonical read to the result limit."""
    from exomem import epistemic_graph, source_capture
    from exomem.governance import lifecycle

    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, encoder = passage_tools
    target = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Related record", source_kind="session",
        passages=[{"key": "target", "text": "The lantern burns oil."}],
    )["source"]["path"]
    anchor = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Anchor record", source_kind="session",
        passages=[{"key": "anchor", "text": f"## Relations\n- supports [[{target}]]"}],
    )["source"]["path"]
    embeddings.index_incremental(vault)
    publish_reference(vault)
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    reads = []
    original = reserved_paths.read_generic_bytes

    def counted(root, path, **kwargs):
        if str(path) in (anchor, target):
            reads.append(str(path))
        return original(root, path, **kwargs)

    authority = source_capture.require_authority
    checks = 0

    def revoke_before_anchor(root):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise source_capture.SourceCaptureError("SOURCE_IDENTITY_FORBIDDEN")
        return authority(root)

    tombstoned = lifecycle.is_tombstoned
    before = len(encoder.texts)
    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", counted)
        if anchor_state == "revoked":
            patch.setattr(source_capture, "require_authority", revoke_before_anchor)
        elif anchor_state == "tombstoned":
            patch.setattr(lifecycle, "is_tombstoned", lambda root, path: path == anchor or tombstoned(root, path))
        reply = call(
            "search_memory",
            {"query": "lantern", "limit": 1, "options": {
                "result_level": "passage", "relations": ["supports"], "relation_of": anchor,
            }},
        )
    if anchor_state == "revoked":
        assert "PASSAGE_AUTHORITY_REQUIRED" in str(reply), reply
        assert reads == [] and len(encoder.texts) == before
        return
    assert "error" not in reply, reply
    expected = ["target"] if anchor_state == "visible" else []
    assert [hit["passage_key"] for hit in reply["hits"]] == expected, reply
    assert set(reads) == ({target} if expected else set()), reads


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
    publish_reference(vault)
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
    """A body declaration must preserve every legal key without a frontmatter budget cost."""
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
    publish_reference(vault)
    reply = call(
        "search_memory",
        {"query": "record 1999", "limit": 200, "options": {"result_level": "passage"}},
    )
    assert "error" not in reply, reply
    assert len(reply["hits"]) == 200
    path = memory_refs.resolve_identifier_read_only(vault, reply["hits"][0]["ref"])
    fields, body, _marker = vault_module.parse_frontmatter(
        vault_module.read_guarded_text(vault, vault / path)[0]
    )
    assert fields["declared_passages"] == 2000
    assert "passage_keys" not in fields
    assert json.dumps([passage["key"] for passage in passages]) in body
    assert all(len(hit["passage_key"].encode()) == 128 for hit in reply["hits"])


def test_frontmatter_size_is_independent_of_declaration_and_capture_text_is_exact(
    vault, passage_tools, monkeypatch
):
    """Keys in metadata slow unrelated readers; keys after Capture corrupt existing text readers."""
    from exomem import temporal

    instant = temporal.now()
    monkeypatch.setattr(temporal, "now", lambda: instant)
    call, _encoder = passage_tools
    frontmatters = []
    for count in (3, 300):
        keys = [f'opaque/{number}\n```\n## Capture\n["\\é' for number in range(count)]
        texts = [f"The lantern record {number}." for number in range(count)]
        saved = commands.op_capture_source(
            vault,
            schema.load_source_schema(vault),
            title="Counted records",
            source_kind="session",
            passages=[dict(key=key, text=text) for key, text in zip(keys, texts, strict=True)],
        )
        assert "error" not in saved, saved
        page = find_corpus.CACHE.get(vault / saved["source"]["path"], vault)
        fields = dict(page.frontmatter)
        fields.pop("exomem_id")  # Each capture has its own immutable identity.
        assert fields.pop("declared_passages") == count
        assert "passage_keys" not in fields
        frontmatters.append(fields)
        assert page.body.split("## Capture\n\n", 1)[1] == "\n\n".join(texts) + "\n"
        from exomem.passage_evidence import declared_chunks

        assert [chunk.key for chunk in declared_chunks(page)] == keys
    assert frontmatters[0] == frontmatters[1]


def test_spaced_passage_cuts_skip_the_per_character_script_scan(vault, passage_tools, monkeypatch):
    """Classifying each character of each long spaced passage made every passage search re-pay seconds."""
    from exomem import text_scripts
    from exomem.passage_evidence import declared_chunks

    over_cap = " ".join(f"lantern{number}" for number in range(embeddings.MAX_WORDS_PER_CHUNK + 1))
    at_cap = " ".join(f"harbour{number}" for number in range(embeddings.MAX_WORDS_PER_CHUNK))
    saved = commands.op_capture_source(
        vault,
        schema.load_source_schema(vault),
        title="Spaced records",
        source_kind="session",
        passages=[dict(key="over", text=over_cap), dict(key="at", text=at_cap)],
    )
    assert "error" not in saved, saved
    page = find_corpus.CACHE.get(vault / saved["source"]["path"], vault)
    page.__dict__.pop("_declared_passage_chunks", None)
    scanned = []
    real = text_scripts.is_scriptio_continua
    monkeypatch.setattr(
        text_scripts, "is_scriptio_continua", lambda character: scanned.append(character) or real(character)
    )

    chunks = declared_chunks(page)

    assert scanned == []
    assert [(chunk.key, chunk.text.split()[0]) for chunk in chunks] == [
        ("over", "lantern0"),
        ("over", f"lantern{embeddings.MAX_WORDS_PER_CHUNK}"),
        ("at", "harbour0"),
    ]


def _mixed_passages(prefix):
    """One passage per cut rule: under the cap, over the word cap, and unspaced over the character cap."""
    return [
        {"key": f"{prefix}/short", "text": "The lantern keeper records oil deliveries."},
        {"key": f"{prefix}/long", "text": " ".join(f"lantern{n}" for n in range(embeddings.MAX_WORDS_PER_CHUNK + 40))},
        {"key": f"{prefix}/unspaced", "text": "灯台の油の記録。" * 90},
    ]


def test_passage_search_reads_the_published_cut_instead_of_cutting_the_source(vault, passage_tools, monkeypatch):
    """Cutting every selected Source again on each search was a fixed cost that four callers queued behind."""
    from exomem import file_watcher, passage_evidence

    call, _encoder = passage_tools
    passages = _mixed_passages("r")
    saved = call("save_source", {"title": "Harbour survey", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "archive", "record_id": "cut", "revision": "one"},
                                             "passages": passages}})
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    watcher = file_watcher.FileWatcher(vault)
    watcher._raw_witness.activate()  # This test drives the existing watcher synchronously.
    assert watcher._flush_raw(census=True)
    cuts = []
    real_cut = passage_evidence.declared_chunks
    monkeypatch.setattr(passage_evidence, "declared_chunks", lambda page: cuts.append(page.rel_path) or real_cut(page))

    reply = call("search_memory", {"query": "lantern oil", "limit": 10, "options": {"result_level": "passage"}})

    assert "error" not in reply, reply
    assert cuts == []
    assert {hit["passage_key"] for hit in reply["hits"]} == {passage["key"] for passage in passages}
    path = memory_refs.resolve_identifier_read_only(vault, reply["hits"][0]["ref"])
    body = vault_module.parse_frontmatter(vault_module.read_guarded_text(vault, vault / path)[0])[1]
    for hit in reply["hits"]:
        passage = passages[hit["passage_ordinal"] - 1]["text"]
        assert hit["passage_hash"] == hashlib.sha256(passage.encode()).hexdigest()
        assert body[hit["span"]["start"] : hit["span"]["start"] + hit["span"]["length"]] in passage


def test_a_published_cut_is_never_applied_to_other_source_bytes(vault, passage_tools):
    """A cut read for bytes it was not made from reports wrong spans, keys and passage hashes."""
    from exomem import capture_inventory, page_names, passage_evidence

    call, _encoder = passage_tools
    pages = []
    for revision, passages in (("one", _mixed_passages("a")), ("two", _mixed_passages("b")[::-1])):
        saved = call("save_source", {"title": "Cut record", "source_kind": "session", "detail": "full",
                                     "options": {"source_identity": {"system": "archive", "record_id": "bound", "revision": revision},
                                                 "passages": passages}})
        assert "error" not in saved, saved
        path = (vault / page_names.resolve_page(vault, saved["source"]["id"])).relative_to(vault).as_posix()
        data = (vault / path).read_bytes()
        pages.append(find_corpus.parse_page(vault / path, 0, vault, content=data, resolved_relative=path))
    first, second = pages
    entry = capture_inventory.input_from_bytes(vault, first.rel_path, (vault / first.rel_path).read_bytes()).entry

    assert list(passage_evidence.source_chunks(first, entry)) == list(passage_evidence.declared_chunks(first))
    assert list(passage_evidence.source_chunks(second, entry)) == list(passage_evidence.declared_chunks(second))


def test_source_bytes_edited_after_publication_are_read_again_not_served_from_memory(
    vault, passage_tools, monkeypatch
):
    """Reading and hashing every selected Source on each search was a third of a search's work; a
    memory of verified passages that outlived an edit would attest the old text instead."""
    # The capture starts a background graph rebuild that can unpublish the corpus after the edit.
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Harbour log", source_kind="session",
        passages=[{"key": "entry", "text": "The lantern burns oil."}],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    request = {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
    first = call("search_memory", request)
    assert [hit["passage_key"] for hit in first["hits"]] == ["entry"], first
    reads = []
    resolve = find._resolve_page
    monkeypatch.setattr(
        find,
        "_resolve_page",
        lambda root, path, *args, **kwargs: reads.append(path) or resolve(root, path, *args, **kwargs),
    )

    assert call("search_memory", request)["hits"] == first["hits"]
    assert saved["source"]["path"] not in reads

    # The owner edits the passage in an editor, and the corpus republishes before the index re-embeds.
    path = vault / saved["source"]["path"]
    path.write_text(path.read_text().replace("The lantern burns oil.", "The lantern burns whale oil."))
    publish_reference(vault)
    edited = call("search_memory", request)

    assert "error" not in edited, edited
    assert edited["hits"] == []
    assert edited["withheld_chunks"] == 1
    assert saved["source"]["path"] in reads


def test_an_erased_source_is_not_served_from_memory_while_the_index_still_selects_it(
    vault, passage_tools, monkeypatch
):
    """An erasure advances the retirement generation once its derived rows are gone. A memory kept
    past that would serve the erased passage to an index that still selects it, as a lagging one can."""
    from exomem import source_custody, writer_lease

    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Erased log", source_kind="session",
        passages=[{"key": "entry", "text": "The lantern burns oil."}],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    request = {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
    first = call("search_memory", request)
    assert [hit["passage_key"] for hit in first["hits"]] == ["entry"], first
    reads = []
    resolve = find._resolve_page
    monkeypatch.setattr(
        find,
        "_resolve_page",
        lambda root, path, *args, **kwargs: reads.append(path) or resolve(root, path, *args, **kwargs),
    )
    assert call("search_memory", request)["hits"] == first["hits"]
    assert saved["source"]["path"] not in reads

    (vault / saved["source"]["path"]).unlink()
    with writer_lease.active_manager().mutation_guard(vault):
        assert source_custody.advance_retirement_generation(vault)
    erased = call("search_memory", request)

    assert "error" not in erased, erased
    assert erased["hits"] == []
    assert erased["withheld_chunks"] == 1


@pytest.mark.parametrize(("bound", "value", "held"), [("max_entries", 2, 2), ("max_bytes", 0, 0)])
def test_the_memory_of_verified_passages_stays_within_its_bound(
    vault, passage_tools, monkeypatch, bound, value, held
):
    """Every Source a search reads adds an entry, so an unbounded memory grows with the corpus."""
    call, _encoder = passage_tools
    for name in ("one", "two", "three"):
        saved = commands.op_capture_source(
            vault, schema.load_source_schema(vault), title=f"Log {name}", source_kind="session",
            passages=[{"key": name, "text": f"The lantern burns {name}."}],
        )
        assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    monkeypatch.setattr(passage_evidence._VERIFIED_SOURCES, bound, value)

    reply = call("search_memory", {"query": "lantern", "limit": 3, "options": {"result_level": "passage"}})

    assert {hit["passage_key"] for hit in reply["hits"]} == {"one", "two", "three"}, reply
    assert len(passage_evidence._VERIFIED_SOURCES) == held


def test_the_memory_of_verified_passages_weighs_at_least_what_it_keeps():
    """Weighing an entry by its frontmatter dict and chunk strings alone counted under half the bytes
    it kept alive, so the 16 MiB bound let the memory hold about 40 MiB in a pod without swap. A
    Source identity's strings have no length limit, so its weight must grow with them."""
    memory = passage_evidence._VerifiedSources(max_entries=1000, max_bytes=1 << 40)
    root, scope, proof = Path("/vault").absolute(), "kb", ("checkpoint", "catalogue", 7)
    for n in range(50):
        chunks = {
            number: passage_evidence.PassageChunk(
                f"record/{n}/{number}", number + 1, hashlib.sha256(f"{n}/{number}".encode()).hexdigest(),
                4000 + 1000 * number, 500, f"{n:03d} {number} " + "word " * 99,
            )
            for number in range(3)
        }
        path = f"Knowledge Base/Sources/Sessions/2026-11-21-source-{n:03d}.md"
        identity = {"system": "example-video", "record_id": f"{n:04d}-" + "x" * 2000, "revision": "1"}
        source = passage_evidence._VerifiedSource(
            f"lh{n:03d}gx.6h5l", passage_evidence._identity_json(identity), chunks
        )
        memory.keep({(root, scope, path, hashlib.sha256(path.encode()).hexdigest()): source}, proof)

    # The root, scope and proof belong to the caller.
    assert memory.held_bytes() >= bytes_kept_alive(memory._entries, root, scope, proof)


def test_a_later_search_for_another_passage_of_a_remembered_source_still_finds_it(vault, passage_tools):
    """The memory keeps only the chunks a search selected. Serving it to a search that selects another
    chunk of that Source would withhold a passage that matches."""
    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Harbour log", source_kind="session",
        passages=[
            {"key": "lamp", "text": "The lantern burns oil."},
            {"key": "bell", "text": "The harbour bell rings at dawn."},
        ],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    lamp = call("search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}})
    assert [hit["passage_key"] for hit in lamp["hits"]] == ["lamp"], lamp

    bell = call("search_memory", {"query": "harbour bell", "limit": 1, "options": {"result_level": "passage"}})

    assert [hit["passage_key"] for hit in bell["hits"]] == ["bell"], bell
    assert bell["withheld_chunks"] == 0


def test_releasing_the_page_cache_releases_the_memory_of_verified_passages(monkeypatch):
    """The idle reclaimer hands page text back through the page cache; a memory it missed would keep
    up to 16 MiB of that text resident in a pod without swap."""
    memory = passage_evidence._VerifiedSources(max_entries=10, max_bytes=1 << 20)
    monkeypatch.setattr(passage_evidence, "_VERIFIED_SOURCES", memory)
    chunk = passage_evidence.PassageChunk("entry", 1, "0" * 64, 0, 22, "The lantern burns oil.")
    memory.keep({("vault", "kb", "Source.md", "0" * 64): passage_evidence._VerifiedSource("id", "null", {0: chunk})}, ())

    find.unload_ram_caches(pages=True)

    assert len(memory) == 0


def test_a_source_identity_holding_a_yaml_set_stays_within_the_memory_bound(vault, passage_tools, monkeypatch):
    """`source_identity` is frontmatter, and YAML parses `!!set` there. Weighing skipped a set's
    members, so a few such Sources held many times the memory's bound in a pod without swap."""
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    memory = passage_evidence._VerifiedSources(max_entries=10, max_bytes=4096)
    monkeypatch.setattr(passage_evidence, "_VERIFIED_SOURCES", memory)
    call, _encoder = passage_tools
    saved = call("save_source", {"title": "Harbour log", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "archive", "record_id": "ledger",
                                                                 "revision": "one"},
                                             "passages": [{"key": "entry", "text": "The lantern burns oil."}]}})
    assert "error" not in saved, saved
    path = vault / page_names.resolve_page(vault, saved["source"]["id"])
    text = path.read_text()
    assert '"revision": "one"}' in text, text
    members = ", ".join(f"member-{n}-" + "x" * 1000 for n in range(20))
    path.write_text(text.replace('"revision": "one"}', '"revision": !!set {' + members + "}}", 1))
    embeddings.index_incremental(vault)
    publish_reference(vault)

    reply = call("search_memory", {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}})

    assert [hit["passage_key"] for hit in reply["hits"]] == ["entry"], reply
    # The vault root, the scope and the proof belong to the search, not to the memory.
    shared = [part for key, (proof, _source) in memory._entries.items() for part in (key[0], key[1], proof)]
    assert bytes_kept_alive(memory._entries, *shared) <= memory.max_bytes


def test_changing_a_returned_source_identity_does_not_change_a_later_answer(vault, passage_tools, monkeypatch):
    """An in-process caller that edits a reply in place, as one merging results may, changed the
    identity that the memory of verified passages gave every later request."""
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, _encoder = passage_tools
    identity = {"system": "archive", "record_id": "ledger", "revision": "one"}
    saved = call("save_source", {"title": "Harbour log", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": identity,
                                             "passages": [{"key": "entry", "text": "The lantern burns oil."}]}})
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    with request_scope(owner_principal()):
        first = commands.op_ask_memory(vault, query="lantern", result_level="passage", limit=1)
        assert [hit["source_identity"] for hit in first["hits"]] == [identity], first
        first["hits"][0]["source_identity"]["record_id"] = "invented"

        later = commands.op_ask_memory(vault, query="lantern", result_level="passage", limit=1)

    assert [hit["source_identity"] for hit in later["hits"]] == [identity], later


def test_an_unpublished_key_edit_is_never_answered_from_memory_or_from_a_fresh_read(
    vault, passage_tools, monkeypatch
):
    """A passage answer attests the bytes the corpus published. An edit that no file event reported
    was withheld while the memory held the Source, but a fresh read answered it as evidence."""
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Harbour log", source_kind="session",
        passages=[{"key": "published-key", "text": "The lantern burns oil."}],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    request = {"query": "lantern", "limit": 1, "options": {"result_level": "passage"}}
    first = call("search_memory", request)
    assert [hit["passage_key"] for hit in first["hits"]] == ["published-key"], first
    path = vault / saved["source"]["path"]
    path.write_text(path.read_text().replace("published-key", "unpublished-key"))

    remembered = call("search_memory", request)
    passage_evidence._VERIFIED_SOURCES.clear()
    fresh = call("search_memory", request)

    assert [hit["passage_key"] for hit in remembered["hits"]] == ["published-key"], remembered
    assert fresh["hits"] == [], fresh
    assert fresh["withheld_chunks"] == 1


def test_a_declared_capture_encodes_each_passage_chunk_once(vault, passage_tools, monkeypatch):
    """The capture's overlap check encoded title-prefixed paragraphs that the index could not
    reuse, so every passage of a capture was encoded twice."""
    from collections import Counter

    from exomem import corpus_aware, page_names, passage_evidence

    call, encoder = passage_tools
    earlier = call("save_source", {"title": "Earlier record", "source_kind": "session", "detail": "full",
                                   "content": "The harbour keeps one lantern lit."})
    assert "error" not in earlier, earlier
    embeddings.index_incremental(vault)  # The overlap check scores against an index with vectors.
    # add() swallows a failed sweep, so a capture that no longer sweeps would still encode once.
    sweeps: list[dict[str, float]] = []
    sweep = corpus_aware._best_cosine_per_file

    def recording_sweep(*args, **kwargs):
        sweeps.append(sweep(*args, **kwargs))
        return sweeps[-1]

    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", recording_sweep)
    passages = [passage for passage in _mixed_passages("once") if not passage["key"].endswith("/long")]
    saved = call("save_source", {"title": "Encoded once", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "archive", "record_id": "once",
                                                                 "revision": "one"},
                                             "passages": passages}})
    assert "error" not in saved, saved
    path = vault / page_names.resolve_page(vault, saved["source"]["id"])
    assert embeddings.upsert_after_write_status(vault, [path], defer_during_warm=False).status == "completed"

    page = find_corpus.parse_page(path, 0, vault, content=path.read_bytes(),
                                  resolved_relative=path.relative_to(vault).as_posix())
    cut = [chunk.text for chunk in passage_evidence.declared_chunks(page)]
    assert len(cut) > len(passages)
    encoded = Counter(encoder.texts)
    assert {text: encoded[text] for text in cut} == dict.fromkeys(cut, 1)
    assert not [text for text in encoder.texts if text.startswith("Encoded once")]
    assert any(page_names.resolve_page(vault, earlier["id"]) in scores for scores in sweeps), sweeps


def test_a_source_of_hundreds_of_declared_passages_is_indexed_within_the_preparation_budget(
    vault, passage_tools, monkeypatch
):
    """Sources of 159 or more passages were refused before chunking and never indexed."""
    from exomem import page_names

    call, encoder = passage_tools
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
    passages = [{"key": f"g/{n:03d}", "text": " ".join(f"w{n}r{w}" for w in range(200))} for n in range(450)]
    saved = call("save_source", {"title": "Large record", "source_kind": "session", "detail": "full",
                                 "options": {"source_identity": {"system": "archive", "record_id": "large",
                                                                 "revision": "one"},
                                             "passages": passages}})
    assert "error" not in saved, saved
    path = vault / page_names.resolve_page(vault, saved["source"]["id"])
    before = len(encoder.texts)

    result = embeddings.upsert_after_write_status(vault, [path], defer_during_warm=False)

    assert result.status == "completed", result
    assert {passage["text"] for passage in passages} <= set(encoder.texts[before:])


def test_same_heading_in_an_ordinary_note_stays_searchable(vault):
    """A format-based global strip would erase authored text in an ordinary note."""
    from exomem.find_types import ParsedPage

    body = (
        '# Authored\n\n## Passage keys\n\n```json\n["visible-marker"]\n```\n\n## Capture\n\nwords\n'
    )
    page = ParsedPage(
        vault / "note.md", "note.md", {"type": "insight"}, body, "Authored", 0, vault_root=vault
    )
    assert page.search_body == body


@pytest.mark.parametrize("mismatch", ["count", "keys", "texts"])
def test_inconsistent_declaration_is_withheld_and_counted(vault, passage_tools, monkeypatch, mismatch):
    """A count or body mismatch must not bind an indexed vector to the wrong caller record."""
    # The capture starts a background graph rebuild whose vault walk can land after the rewrite
    # below; that unpublishes the corpus, and the search is refused before it reads the declaration.
    monkeypatch.setenv("EXOMEM_DISABLE_GRAPH_SCHEDULING", "1")
    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault,
        schema.load_source_schema(vault),
        title="Broken declaration",
        source_kind="session",
        passages=[{"key": "first", "text": "lantern oil"}, {"key": "second", "text": "keeper oil"}],
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    path = vault / saved["source"]["path"]
    source = path.read_text()
    assert "declared_passages: 2" in source
    if mismatch == "count":
        source = source.replace("declared_passages: 2", "declared_passages: 3")
    elif mismatch == "keys":
        source = source.replace(json.dumps(["first", "second"]), json.dumps(["first"]))
    else:
        source = source.removesuffix("keeper oil\n")
    path.write_text(source)
    reply = call(
        "search_memory", {"query": "oil", "limit": 2, "options": {"result_level": "passage"}}
    )
    assert "error" not in reply, reply
    assert reply["hits"] == []
    assert reply["withheld_chunks"] == 2


def test_passage_request_reads_only_selected_sources_and_observes_new_capture(
    vault, passage_tools, monkeypatch, record_property
):
    """A per-request header scan costs the whole vault; a stale set hides the next capture."""
    call, _encoder = passage_tools
    saved = commands.op_capture_source(
        vault,
        schema.load_source_schema(vault),
        title="Reference records",
        source_kind="session",
        passages=[{"key": "old", "text": "lantern oil"}],
    )
    assert "error" not in saved, saved
    first = vault / saved["source"]["path"]
    template = first.read_text()
    identity = vault_module.parse_frontmatter(template)[0]["exomem_id"]
    for number in range(1, 200):
        (first.parent / f"reference-{number}.md").write_text(
            template.replace(identity, str(uuid4()))
        )
    embeddings.index_incremental(vault)
    publish_reference(vault)
    warm = call(
        "search_memory", {"query": "lantern", "limit": 5, "options": {"result_level": "passage"}}
    )
    assert len(warm["hits"]) == 5, warm
    reads = []
    read_bytes, open_binary = reserved_paths.read_generic_bytes, reserved_paths.open_generic_binary

    def count_read(root, path, **kwargs):
        if str(path).startswith(first.parent.relative_to(vault).as_posix() + "/"):
            reads.append(str(path))
        return read_bytes(root, path, **kwargs)

    def count_header(root, path, **kwargs):
        if str(path).startswith(first.parent.relative_to(vault).as_posix() + "/"):
            reads.append(str(path))
        return open_binary(root, path, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", count_read)
        patch.setattr(reserved_paths, "open_generic_binary", count_header)
        reply = call(
            "search_memory",
            {"query": "lantern", "limit": 5, "options": {"result_level": "passage"}},
        )
    assert len(reply["hits"]) == 5, reply
    record_property("declared_sources", 200)
    record_property("source_reads", len(reads))
    record_property("distinct_sources_read", len(set(reads)))
    assert len(set(reads)) <= 5, reads
    assert set(reads) <= {
        memory_refs.resolve_identifier_read_only(vault, hit["ref"]) for hit in reply["hits"]
    }
    new = call(
        "save_source",
        {
            "title": "Meridian",
            "source_kind": "session",
            "options": {"passages": [{"key": "new", "text": "meridian"}]},
        },
    )
    assert "error" not in new, new
    embeddings.index_incremental(vault)
    reads.clear()
    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", count_read)
        patch.setattr(reserved_paths, "open_generic_binary", count_header)
        reply = call(
            "search_memory",
            {"query": "meridian", "limit": 1, "options": {"result_level": "passage"}},
        )
    assert [hit["passage_key"] for hit in reply["hits"]] == ["new"], reply
    assert len(set(reads)) <= 1, reads
    assert set(reads) <= {
        memory_refs.resolve_identifier_read_only(vault, hit["ref"]) for hit in reply["hits"]
    }
    record_property("source_reads_after_capture", len(reads))


def test_full_model_admission_refuses_passage_request_without_encoding(passage_tools, monkeypatch):
    """A full cell must return MODEL_BUSY with a retry hint before it queues or encodes."""
    call, encoder = passage_tools
    gate = runtime_resources.ModelAdmissionGate(1)
    monkeypatch.setattr(runtime_resources, "_process_gate", lambda: gate)
    ready, release = threading.Event(), threading.Event()

    def occupy():
        with gate.admission():
            ready.set()
            assert release.wait(10)

    before = len(encoder.texts)
    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(occupy)
        assert ready.wait(5)
        try:
            request = pool.submit(
                call, "search_memory", {"query": "lantern", "options": {"result_level": "passage"}}
            )
            refused = request.result(timeout=5)
            assert "MODEL_BUSY" in str(refused), refused
            assert "retry" in str(refused).lower(), refused
            assert len(encoder.texts) == before
        finally:
            release.set()
        holder.result()


def test_abandoned_passage_search_stops_before_it_encodes(vault, passage_tools, monkeypatch):
    """A caller that times out must end its search, not leave it running unseen in its admission slot."""
    call, encoder = passage_tools
    saved = call(
        "save_source",
        {
            "title": "Survey",
            "source_kind": "session",
            "tags": ["readers"],
            "options": {"passages": [{"key": "record", "text": "The lantern burns oil."}]},
        },
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    walking, caller_left = threading.Event(), threading.Event()
    walk = find._managed_eligible_filter_paths

    def walk_that_outlasts_its_caller(*args, **kwargs):
        walking.set()
        deadline = time.monotonic() + 10
        while not caller_left.is_set() and time.monotonic() < deadline:
            try:
                # The serving host's cancel scope is where a departed caller is recorded.
                anyio.from_thread.check_cancelled()
            except asyncio.CancelledError:
                caller_left.set()
            time.sleep(0.01)
        return walk(*args, **kwargs)

    monkeypatch.setattr(find, "_managed_eligible_filter_paths", walk_that_outlasts_its_caller)
    mcp = server.build_server(require_auth=False)
    before = len(encoder.texts)

    async def search_then_leave():
        async with Client(mcp) as client:
            async with anyio.create_task_group() as caller:
                caller.start_soon(
                    client.call_tool,
                    "search_memory",
                    {"query": "lantern", "tags": ["readers"], "options": {"result_level": "passage"}},
                )
                assert await anyio.to_thread.run_sync(walking.wait, 10)
                caller.cancel_scope.cancel()

    asyncio.run(search_then_leave())
    assert caller_left.is_set()
    assert encoder.texts[before:] == []


def test_a_capture_encode_does_not_hold_up_a_passage_query_on_a_concurrent_encoder(
    vault, passage_tools, monkeypatch
):
    """A capture encodes its Source one inference per shared turn; a search must not queue behind each one."""
    call, encoder = passage_tools
    saved = call(
        "save_source",
        {
            "title": "Survey",
            "source_kind": "session",
            "tags": ["readers"],
            "options": {"passages": [{"key": "record", "text": "The lantern burns oil."}]},
        },
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    # The served ONNX encoder runs inferences side by side; the fixture's plain model does not. The
    # query runs the real encode path, which picks the turn each inference takes.
    concurrent = SimpleNamespace(
        texts_fit=lambda _texts: True,
        concurrent_encodes=True,
        encode=lambda texts, **_options: encoder(texts),
    )
    monkeypatch.setattr(embeddings, "_MODEL", concurrent)
    monkeypatch.setattr(embeddings, "get_model", lambda: concurrent)
    monkeypatch.setattr(embeddings, "embed_texts", REAL_EMBED_TEXTS)
    monkeypatch.setattr(embeddings, "_QUERY_TURN_GATE", None)
    shared = runtime_resources.ModelAdmissionGate(2)
    monkeypatch.setattr(runtime_resources, "_process_gate", lambda: shared)
    inferring, release = threading.Event(), threading.Event()

    def capture_inference():
        # A capture holds one admission for its Source and the shared turn for each inference.
        with shared.admission(), shared.execution():
            inferring.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        capture = pool.submit(capture_inference)
        assert inferring.wait(5)
        try:
            search = pool.submit(
                call,
                "search_memory",
                {"query": "lantern", "tags": ["readers"], "options": {"result_level": "passage"}},
            )
            reply = search.result(timeout=5)
        finally:
            release.set()
        capture.result()
    assert [hit["passage_key"] for hit in reply["hits"]] == ["record"], reply
    assert shared.admitted_count() == 0


def test_abandoned_passage_search_leaves_its_model_turn_queue_at_once(vault, passage_tools, monkeypatch):
    """A search queued behind another encode must free its admission when its caller leaves, not after that encode."""
    call, encoder = passage_tools
    saved = call(
        "save_source",
        {
            "title": "Survey",
            "source_kind": "session",
            "tags": ["readers"],
            "options": {"passages": [{"key": "record", "text": "The lantern burns oil."}]},
        },
    )
    assert "error" not in saved, saved
    embeddings.index_incremental(vault)
    publish_reference(vault)
    with monkeypatch.context() as service:
        # Cells run the service profile, whose turn queue is the one a waiter can leave.
        service.setenv("EXOMEM_CLOUD_CELL", "1")
        service.setenv("EXOMEM_CLOUD_RESOURCE_POLICY", "service-v1")
        gate = runtime_resources.ModelAdmissionGate(2)
    monkeypatch.setattr(runtime_resources, "_process_gate", lambda: gate)
    holding, release = threading.Event(), threading.Event()

    def other_encode():
        with gate.admission(), gate.execution():
            holding.set()
            assert release.wait(10)

    def settles(condition, seconds):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)
        return condition()

    mcp = server.build_server(require_auth=False)
    before = len(encoder.texts)

    async def search_then_leave():
        async with Client(mcp) as client:
            async with anyio.create_task_group() as caller:
                caller.start_soon(
                    client.call_tool,
                    "search_memory",
                    {"query": "lantern", "tags": ["readers"], "options": {"result_level": "passage"}},
                )
                queued = lambda: gate.waiting_counts()["foreground"] == 1  # noqa: E731
                assert await anyio.to_thread.run_sync(settles, queued, 10)
                caller.cancel_scope.cancel()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(other_encode)
        assert holding.wait(5)
        try:
            asyncio.run(search_then_leave())
            # Only the other encode still holds the gate while it runs.
            left = lambda: gate.waiting_counts()["foreground"] == 0 and gate.admitted_count() == 1  # noqa: E731
            assert settles(left, 5)
        finally:
            release.set()
        holder.result()
    assert encoder.texts[before:] == []


def test_non_owner_cannot_bypass_passage_authority_with_an_admission_predicate(
    vault, passage_tools, monkeypatch
):
    """A caller-supplied allow-all predicate must not expose hidden Source metadata."""
    from exomem.governance.principal import RequestPrincipal, request_scope

    _call, encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Bound records", source_kind="session",
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    parent = Path(saved["source"]["path"]).parent.as_posix() + "/"
    reads = []
    original = reserved_paths.read_generic_bytes

    def counted(root, path, **kwargs):
        if str(path).startswith(parent):
            reads.append(str(path))
        return original(root, path, **kwargs)

    monkeypatch.setattr(reserved_paths, "read_generic_bytes", counted)
    before = len(encoder.texts)
    reader = RequestPrincipal(audience_id="principal:reader", surface="cli", resolved=True)
    with request_scope(reader):
        with pytest.raises(cli_ops.OpError) as public:
            commands.op_ask_memory(vault, query="lantern", result_level="passage")
        with pytest.raises(cli_ops.OpError) as internal:
            find.find(vault, query="lantern", result_level="passage", admit_path=lambda path: True)
    assert public.value.code == internal.value.code == "PASSAGE_AUTHORITY_REQUIRED"
    assert reads == []
    assert len(encoder.texts) == before


@pytest.mark.parametrize("state", ["cold", "configuration", "generation"])
def test_unavailable_publication_reads_no_source_and_encoding_waits_for_publication(
    vault, passage_tools, monkeypatch, state
):
    """A cold or invalid publication must not turn a request into a corpus rebuild."""
    call, encoder = passage_tools
    saved = commands.op_capture_source(
        vault, schema.load_source_schema(vault), title="Published record", source_kind="session",
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    embeddings.index_incremental(vault)
    publish_reference(vault)
    if state == "cold":
        semantic_contract.reset_corpus_context_cache()
    elif state == "configuration":
        configured = semantic_contract._config_inputs(vault)[0]
        configured.parent.mkdir(parents=True, exist_ok=True)
        configured.write_text("{}\n")
    elif state == "generation":
        # A watcher restart keeps file signatures but retires the old publication caption.
        freshness.seed(vault, "vault", [
            (str(path), freshness.stat_signature(path)) for path in find._corpus_walk(vault, "vault")
        ])

    parent = Path(saved["source"]["path"]).parent.as_posix() + "/"
    reads = []
    original = reserved_paths.read_generic_bytes

    def counted(root, path, **kwargs):
        if str(path).startswith(parent):
            reads.append(str(path))
        return original(root, path, **kwargs)

    before = len(encoder.texts)
    with monkeypatch.context() as patch:
        patch.setattr(reserved_paths, "read_generic_bytes", counted)
        reply = call(
            "search_memory", {"query": "lantern", "options": {"result_level": "passage"}}
        )
    assert "PASSAGE_INDEX_UNAVAILABLE" in str(reply), reply
    assert "warm-up" in str(reply), reply
    assert reads == []
    assert len(encoder.texts) == before
    publish_reference(vault)
    ready_reply = call(
        "search_memory", {"query": "lantern", "options": {"result_level": "passage"}}
    )
    assert [hit["passage_key"] for hit in ready_reply["hits"]] == ["record"], ready_reply
