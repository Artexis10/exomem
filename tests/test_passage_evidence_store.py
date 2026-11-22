"""Declared keys travel in canonical store bytes when Markdown projections are absent."""

# Imported fixtures are requested by each test's arguments.
# ruff: noqa: F811
from __future__ import annotations

import sys
from pathlib import PurePosixPath
from uuid import uuid4

import numpy as np
import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_embeddings import catch_up, encoder  # noqa: F401
from test_vault_store_migration import vault  # noqa: F401
from test_vault_store_read_write import put, stored, writer  # noqa: F401
from test_vault_store_write_cost import grow
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, embeddings, find_corpus, memory_refs, schema, semantic_contract
from exomem import vault as vault_module
from exomem.governance.principal import owner_principal, request_scope
from exomem.vault_store import read, write

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="The offline-owner lease is Linux-only"
)


@pytest.fixture(autouse=True)
def published_corpus_cache(monkeypatch):
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)


@pytest.fixture
def bound_store_search(served, encoder):
    """Use the MCP command binding; this build does not start a store-mode service."""
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "ask_memory")
    bound = commands.bind_vault(command.leaf, served, command=command)

    def call(**arguments):
        with request_scope(owner_principal()):
            payload = bound(**arguments)
        return payload.get("result", payload)

    return call, encoder


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
    semantic_contract.warm_corpus_context(served)
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
    semantic_contract.warm_corpus_context(served)
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


def test_bound_store_passages_read_only_the_selected_sources(
    served, writer, bound_store_search, monkeypatch, record_property
):
    """A corpus scan or candidate admission must not hydrate every stored Source."""
    saved = commands.op_capture_source(
        served, schema.load_source_schema(served), title="Stored reference", source_kind="session",
        tags=["readers"],
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    path = saved["source"]["path"]
    with read.snapshot(served) as snapshot:
        template = snapshot.page(path).body
    identity = vault_module.parse_frontmatter(template.decode())[0]["exomem_id"]
    parent = str(PurePosixPath(path).parent) + "/"
    puts = tuple(
        write.PagePut(
            f"{parent}reference-{number}.md",
            template.replace(identity.encode(), str(uuid4()).encode(), 1),
        )
        for number in range(1, 200)
    )
    write.commit(writer, write.StoreChange(write.Origin.GOVERNED, "reference", puts=puts))
    catch_up(served)
    semantic_contract.warm_corpus_context(served)
    assert not (served / path).exists()
    reads = []
    one, many, source = read.StoreSnapshot.page, read.StoreSnapshot.page_sources, read.page_source

    def counted_one(snapshot, rel):
        if rel.startswith(parent):
            reads.append(rel)
        return one(snapshot, rel)

    def counted_many(snapshot):
        for page in many(snapshot):
            if page.meta.path.startswith(parent):
                reads.append(page.meta.path)
            yield page

    def counted_source(reader, root, rel):
        if rel.startswith(parent):
            reads.append(rel)
        return source(reader, root, rel)

    call, _encoder = bound_store_search
    with monkeypatch.context() as patch:
        patch.setattr(read.StoreSnapshot, "page", counted_one)
        patch.setattr(read.StoreSnapshot, "page_sources", counted_many)
        patch.setattr(read, "page_source", counted_source)
        reply = call(query="lantern", tags=["readers"], limit=5, result_level="passage")
    assert "error" not in reply, reply
    assert len(reply["hits"]) == 5
    assert len(set(reads)) <= 5, reads
    assert set(reads) <= {
        memory_refs.resolve_identifier_read_only(served, hit["ref"]) for hit in reply["hits"]
    }
    record_property("declared_sources", 200)
    record_property("source_body_reads", len(reads))
    record_property("distinct_sources_read", len(set(reads)))


def test_store_revision_retires_borrowed_metadata_without_a_markdown_event(
    served, writer, bound_store_search
):
    """A store commit must retire stale declaration metadata even with no file projection.

    A commit that names more pages than the corpus advance carries (``_PAGE_DELTA_LIMIT``) leaves the
    resident corpus at the earlier revision, so the borrowed metadata must be refused until a rebuild.
    """
    saved = commands.op_capture_source(
        served, schema.load_source_schema(served), title="Revision reference", source_kind="session",
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    catch_up(served)
    semantic_contract.warm_corpus_context(served)
    grow(writer, 1, semantic_contract._PAGE_DELTA_LIMIT + 1)
    assert not (served / saved["source"]["path"]).exists()
    call, encoder = bound_store_search
    before = len(encoder.texts)
    refused = call(query="lantern", result_level="passage")
    assert "PASSAGE_INDEX_UNAVAILABLE" in str(refused), refused
    assert len(encoder.texts) == before
    semantic_contract.warm_corpus_context(served)
    ready = call(query="lantern", result_level="passage")
    assert [hit["passage_key"] for hit in ready["hits"]] == ["record"], ready


def test_a_store_commit_moves_the_published_declarations_by_the_pages_it_names(
    served, writer, bound_store_search
):
    """Defect that this guards: a published corpus that a commit leaves behind (the passage index is refused
    after a commit of an unrelated page), or that a commit moves forward without the pages that it names (a
    source captured after the warm-up is missing from the declarations). With no Markdown file, the index keeps
    serving after an unrelated commit, and a source captured by a commit is searchable at once."""
    from test_vault_store_migration import ALPHA

    commands.op_capture_source(
        served, schema.load_source_schema(served), title="Revision reference", source_kind="session",
        passages=[{"key": "record", "text": "The lantern burns oil."}],
    )
    catch_up(served)
    semantic_contract.warm_corpus_context(served)
    call, _encoder = bound_store_search
    with read.snapshot(served) as snapshot:
        unrelated = snapshot.page(ALPHA).body
    write.commit(writer, put(ALPHA, unrelated + b"\nAn unrelated stored edit.\n"))
    after_unrelated = call(query="lantern", result_level="passage")
    assert [hit["passage_key"] for hit in after_unrelated["hits"]] == ["record"], after_unrelated
    later = commands.op_capture_source(
        served, schema.load_source_schema(served), title="Later reference", source_kind="session",
        passages=[{"key": "second", "text": "The keeper trims the wick."}],
    )
    catch_up(served)
    assert not (served / later["source"]["path"]).exists()
    after_capture = call(query="wick", result_level="passage")
    assert "second" in [hit["passage_key"] for hit in after_capture.get("hits", [])], after_capture


def test_import_retry_binds_the_original_that_preceded_the_store_commit(served, tmp_path, monkeypatch):
    import hashlib

    from exomem import client_artifacts

    original = b"Complete record\x00\xff"
    digest = hashlib.sha256(original).hexdigest()
    installed = []

    class Interrupted(BaseException):
        pass

    def stop_at_original(path):
        if path.suffix == ".txt" and path.is_relative_to(served) and (
            vault_module.in_append_only_tree(path.relative_to(served).as_posix()) == "Sources"
        ):
            installed.append(path)
            raise Interrupted()

    def invoke():
        staged = tmp_path / "staged"
        staged.write_bytes(original)
        with request_scope(owner_principal()):
            return client_artifacts._commit_staged_sources(
                served, source_schema=schema.load_source_schema(served), title="Survey record",
                staged={0: client_artifacts.StagedArtifact("transfer", staged, len(original), digest,
                                                           "text/plain", "survey.txt")},
                outcomes=[None], source_fields={"source_type": "session"}, import_key="a" * 64,
            )

    with monkeypatch.context() as patch:
        patch.setattr(vault_module, "_after_batch_destination_published", stop_at_original)
        with pytest.raises(Interrupted):
            invoke()
    [target] = installed
    version = target.stat().st_ino
    assert target.read_bytes() == original
    with read.snapshot(served) as snapshot:
        assert snapshot.page(f"{target.relative_to(served).as_posix()}.md") is None

    [stored] = invoke()["files"]
    assert stored["outcome"] == "stored"
    assert stored["hash"] == digest
    assert target.read_bytes() == original and target.stat().st_ino == version
    with read.snapshot(served) as snapshot:
        assert snapshot.page(stored["page"]) is not None
    [replay] = invoke()["files"]
    assert replay["state"] == "already_stored" and replay["ref"] == stored["ref"]
