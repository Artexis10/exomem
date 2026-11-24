"""`read_page` lists the rows and plan items that cite the page, and pages onward without re-reading it.

The fixtures reuse the link-values log, where each row's `page` field cites a page by its ref.
"""

import pytest
from test_collection_link_values import (
    ALPHA,
    HIDDEN,
    K1,
    K2,
    K3,
    LOG,
    _ready,
    append,
    log_text,
    page,
)
from test_collection_row_references import path
from test_collection_store_writer import store as store  # noqa: F401
from test_page_citations import publish

from exomem import commands
from exomem.collection_store import connection
from exomem.governance.principal import owner_principal, request_scope

ALPHA_REF = f"exomem://memory/{ALPHA}"


@pytest.fixture
def world(store, monkeypatch, corpus_cache_on):
    monkeypatch.setattr(connection, "store_path", lambda root: store.handle.path)
    page(store.root, "Notes/Alpha.md", "Alpha", ALPHA)
    publish(store.root)
    store.create_collection(path("Log"), log_text(), why="declare", scaffold=False)
    return store


def read(world, target="Notes/Alpha.md", **options):
    with request_scope(owner_principal()):
        return commands.op_read_memory(world.root, target, **options)


def test_a_page_read_lists_its_citing_rows_and_pages_onward_without_reading_the_page_again(world, monkeypatch):
    for key in (K1, K2, K3):
        append(world, {"page": "Alpha"}, key=key)
    _ready(world)
    monkeypatch.setattr(commands, "_CITED_BY_LIMIT", 2)
    first = read(world)
    assert first["body"] and first["cited_by"]["count"] == 3
    assert [item["label"] for item in first["cited_by"]["items"]] == ["c1", "c2"]

    def never_read_again(*args, **kwargs):
        raise AssertionError("the page was read again")

    monkeypatch.setattr(commands, "op_get", never_read_again)
    rest = read(world, continuation=first["cited_by"]["more"])
    assert list(rest) == ["cited_by"]
    assert [item["label"] for item in rest["cited_by"]["items"]] == ["c3"]
    assert rest["cited_by"]["more"] is None


def test_a_warming_index_reads_as_warming_never_as_no_citations(world):
    append(world, {"page": "Alpha"})
    assert read(world)["cited_by"] == {"state": "warming"}


def test_a_scan_of_frontmatter_and_a_vault_without_a_store_carry_no_citations(world, monkeypatch):
    append(world, {"page": "Alpha"})
    _ready(world)
    assert "cited_by" not in read(world, frontmatter_only=True)
    monkeypatch.setattr(connection, "store_path", lambda root: root / "no-store.sqlite")
    assert "cited_by" not in read(world)


def test_a_continuation_for_a_withheld_page_answers_exactly_like_one_for_a_missing_page(world):
    """They answer alike while the index warms too: the state of either reply comes from the vault alone."""
    page(world.root, "Private/Secret.md", "Secret", HIDDEN)
    publish(world.root)
    append(world, {"page": f"exomem://memory/{HIDDEN}"})
    (world.root / "Knowledge Base/_access.yaml").write_text('excluded: ["Private/Secret.md"]\n')
    cursor = commands.page_citations_module._encode((LOG, "page", 1))

    def withheld_and_missing():
        return (read(world, "Knowledge Base/Private/Secret.md", continuation=cursor),
                read(world, "Knowledge Base/Private/Nothing.md", continuation=cursor))

    assert withheld_and_missing() == ({"cited_by": {"state": "warming"}},) * 2
    _ready(world)
    assert withheld_and_missing() == ({"cited_by": {"state": "ready", "items": [], "count": 0, "more": None}},) * 2


def test_a_continuation_that_is_no_citation_cursor_is_refused_as_every_continuation_is(world):
    append(world, {"page": "Alpha"})
    _ready(world)
    with pytest.raises(ValueError, match="INVALID_CONTINUATION"):
        read(world, continuation="not-a-cursor")
