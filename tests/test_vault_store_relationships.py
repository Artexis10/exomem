"""The store commits relationship history and projection work with its page changes."""

import sqlite3

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_read_write import rows, stored, stored_body, writer  # noqa: F401

from exomem.vault_store import projector, relationships, write


def test_relationship_only_commit_queues_both_pages_without_changing_their_bytes(
    stored, writer,  # noqa: F811
):
    first = b"# Alpha\n\n- [rule] Check the tide. ^tide\n"
    second = b"# Beta\n\n- [rule] Light the lamp. ^lamp\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the rules.",
        puts=(write.PagePut(ALPHA, first), write.PagePut(BETA, second)),
    ))
    projector.Projector(stored, run=lambda work: work(writer)).run_pass()
    assert rows(stored, "SELECT path FROM store_projection_queue") == []

    saved = write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Link the two rules.",
        relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),
                        relationships.UnitIdentity("lamp-id", BETA, "lamp")),
            added=(relationships.Relationship("edge-id", "tide-id", "supports", "lamp-id"),),
        ),
    ))

    assert stored_body(stored, ALPHA) == first
    assert stored_body(stored, BETA) == second
    assert rows(stored, "SELECT path, revision FROM store_projection_queue ORDER BY path") == [
        (ALPHA, saved.revision), (BETA, saved.revision),
    ]
    assert rows(stored, "SELECT source_unit_id, target_unit_id FROM store_relationships") == [
        ("tide-id", "lamp-id"),
    ]
    assert rows(stored, "SELECT action, actor, reason FROM store_relationship_history") == [
        ("add", "owner", "Link the two rules."),
    ]


def test_a_relationship_commit_with_a_missing_end_leaves_page_and_identity_unchanged(
    stored, writer,  # noqa: F811
):
    original = stored_body(stored, ALPHA)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "Link the tide rule.",
            puts=(write.PagePut(ALPHA, original + b"\n- [rule] Check the tide. ^tide\n"),),
            relationship_change=relationships.Change(
                actor="owner",
                identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),),
                added=(relationships.Relationship("edge-id", "tide-id", "supports", "missing"),),
            ),
        ))
    assert stored_body(stored, ALPHA) == original
    assert rows(stored, "SELECT id FROM store_unit_ids") == []
    assert rows(stored, "SELECT relationship_id FROM store_relationship_history") == []
    assert rows(stored, "SELECT path FROM store_projection_queue") == []
