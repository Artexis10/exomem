"""The store commits relationship history and projection work with its page changes."""

import sqlite3

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_read_write import rows, stored, stored_body, writer  # noqa: F401
from test_vault_store_write_path import served  # noqa: F401

from exomem import commands, observe_memory, reserved_paths, semantic_index, unit_edges, vault_portability
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance.principal import library_scope
from exomem.vault_store import importer, projector, relationship_files, relationships, write


def test_relationship_only_commit_queues_both_pages_without_changing_their_bytes(
    served, writer,  # noqa: F811
):
    first = b"# Alpha\n\n- [rule] Check the tide. ^tide\n"
    second = b"# Beta\n\n- [rule] Light the lamp. ^lamp\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the rules.",
        puts=(write.PagePut(ALPHA, first), write.PagePut(BETA, second)),
    ))
    projector.Projector(served, run=lambda work: work(writer)).run_pass()
    assert rows(served, "SELECT path FROM store_projection_queue") == []

    vault_module.batch_atomic_write(
        (), vault_root=served,
        _relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),
                        relationships.UnitIdentity("lamp-id", BETA, "lamp")),
            added=(relationships.Relationship("edge-id", "tide-id", "supports", "lamp-id"),),
            reason="Link the two rules.",
        ),
    )

    assert stored_body(served, ALPHA) == first
    assert stored_body(served, BETA) == second
    [(source, target, revision)] = rows(
        served, "SELECT source_unit_id, target_unit_id, created_revision FROM store_relationships",
    )
    assert (source, target) == ("tide-id", "lamp-id")
    assert rows(served, "SELECT path, revision FROM store_projection_queue ORDER BY path") == [
        (ALPHA, revision), (BETA, revision),
    ]
    assert rows(served, "SELECT action, actor, reason FROM store_relationship_history") == [
        ("add", "owner", "Link the two rules."),
    ]
    projected = projector.Projector(served, run=lambda work: work(writer)).run_pass()
    assert not projected.deferred
    assert rows(served, "SELECT path FROM store_projection_queue") == []
    for path, source in ((ALPHA, first), (BETA, second)):
        raw = (served / path).read_bytes()
        assert relationship_files.strip_block(raw) == source
        assert b"Stored relationships" in raw
    artifact = served / relationship_files.path_for(ALPHA)
    with artifact.open("rb") as encoded:
        recovered = relationship_files.parse(encoded)
    assert recovered.relationships[0].id == "edge-id"
    assert recovered.target_units[0].anchor == "lamp"


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


def test_assess_anchors_the_far_end_and_later_changes_only_relationships(served, writer):  # noqa: F811
    source_path = "Knowledge Base/Notes/Observations/Alpha Report.md"
    target_path = "Knowledge Base/Notes/Observations/Beta Report.md"
    alpha = (
        "---\ntitle: Alpha Report\ntype: insight\nstatus: active\n"
        "exomem_id: e8f840b6-3cf8-44cf-ab95-12d05683a194\nupdated: 2026-07-15\n---\n\n"
        "# Alpha\n\n## Claim\n- category: rule\n- id: tide\n\nCheck the tide.\n"
    ).replace("\n", "\r\n").encode()
    beta = (
        "---\ntitle: Beta Report\ntype: insight\nstatus: active\n"
        "exomem_id: 04e56772-21a9-44ff-b0df-0a51ed0e166b\n---\n\n"
        "# Beta\n\n- [rule] Light the lamp.\n"
    ).replace("\n", "\r\n").encode()
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the observations.",
        puts=(write.PagePut(source_path, alpha), write.PagePut(target_path, beta)),
    ))
    old = semantic_index.selected_parent_index_state(served, target_path).document.units[0]
    fragment = old.unit_ref.partition("#")[2]
    assert fragment and old.anchor is None
    claim = semantic_index.selected_parent_index_state(served, source_path).document.units[0]
    claim_id = observe_memory.unit_id(claim)

    update = dict(
        path=source_path, operation="assess", unit=claim_id,
        relations=[{"kind": "supports", "target": f"[[Beta Report#{fragment}]]"}],
        reason="Record the support.",
    )
    preview = commands.op_observe_memory(served, **update, validate_only=True)
    assert not preview["mutated"] and stored_body(served, target_path) == beta
    commands.op_observe_memory(served, **update, transition_token=preview["semantic"]["transition_token"])

    assert stored_body(served, source_path) == alpha
    [(anchor,)] = rows(served, "SELECT anchor FROM store_unit_ids WHERE parent_path = ?", target_path)
    assert stored_body(served, target_path) == beta.replace(b"Light the lamp.", f"Light the lamp. ^{anchor}".encode())
    assert rows(served, "SELECT reason FROM store_relationship_history") == [("Record the support.",)]
    page_revisions = "SELECT path,revision,sha256 FROM store_pages WHERE path IN (?,?) ORDER BY path"
    before = rows(served, page_revisions, source_path, target_path)
    commands.op_observe_memory(
        served, path=source_path, operation="assess", unit=claim_id,
        relations=[{"kind": "contradicts", "target": f"[[Beta Report#{anchor}]]"}],
        reason="Record the conflicting observation.",
    )
    after = rows(served, page_revisions, source_path, target_path)
    assert before == after
    assert rows(served, "SELECT relation_type FROM store_relationships ORDER BY relation_type") == [
        ("contradicts",), ("supports",),
    ]
    with library_scope():
        page = unit_edges.relationship_page(served, source_path, "tide", limit=1)
        remaining = unit_edges.relationship_page(served, source_path, "tide", limit=1, after=page.next_after)
    assert page.total == remaining.total == 2
    assert page.next_after is not None and remaining.next_after is None
    assert [edge.relationship.relation_type for edge in (*page.edges, *remaining.edges)] == [
        "contradicts", "supports",
    ]
    assert rows(
        served, "SELECT COUNT(*) FROM graph_edges WHERE edge_key LIKE 'stored:%' AND source_path=?", source_path,
    ) == [(2,)]


def test_relation_file_carries_locators_and_keeps_managed_block_out_of_page_bytes():
    page = b"\xef\xbb\xbf---\r\ntitle: Tide\r\n---\r\n\r\n# Tide\r\n\r\nThe tide is high."
    projected = relationship_files.splice_block(page, ["### Stored relationships", "- Outgoing: 1"])
    assert relationship_files.strip_block(projected) == page
    altered = projected.replace(b"Outgoing: 1", b"Outgoing: 7")
    with pytest.raises(OpError, match="managed relations block was edited"):
        relationship_files.strip_block(altered)
    assert relationship_files.strip_block(altered, keep_mine=True) == page
    with pytest.raises(OpError, match="markers are damaged"):
        relationship_files.strip_block(
            altered.replace(b"evermatter:relations-end", b"evermatter:relations-broken"),
            keep_mine=True,
        )
    source = relationships.UnitIdentity("tide-id", ALPHA, "tide", "exomem://memory/first")
    target = relationships.UnitIdentity("lamp-id", BETA, "lamp", "exomem://memory/second")
    link = relationships.Relationship("edge-id", source.id, "supports", target.id)
    encoded = relationship_files.lines(
        ALPHA, 12, (source,), (relationships.BoundRelationship(link, source, target),),
    )
    decoded = relationship_files.parse(encoded)
    assert decoded == relationship_files.PortableRelations(ALPHA, 12, (source,), (link,), (target,))
    path = relationship_files.path_for(ALPHA)
    assert reserved_paths.classify_logical(path).blocked
    assert vault_portability.classify_artifact(path).artifact_class is vault_portability.ArtifactClass.CANONICAL


def test_a_relations_file_edit_keeps_history_and_an_edited_block_is_held(served, writer):  # noqa: F811
    first = b"# Alpha\n\n- [rule] Check the tide. ^tide\n"
    second = b"# Beta\n\n- [rule] Light the lamp. ^lamp\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the rules.",
        puts=(write.PagePut(ALPHA, first), write.PagePut(BETA, second)),
        relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),
                        relationships.UnitIdentity("lamp-id", BETA, "lamp")),
            added=(relationships.Relationship("old-id", "tide-id", "supports", "lamp-id"),),
        ),
    ))
    projection = projector.Projector(served, run=lambda work: work(writer))
    assert not projection.run_pass().deferred
    artifact = relationship_files.path_for(ALPHA)
    rel_file = served / artifact
    original = rel_file.read_bytes()
    rel_file.write_bytes(original.replace(b'"supports"', b'"contradicts"'))
    watcher = importer.Importer(served, run=lambda work: work(writer))
    result = watcher.import_paths([artifact])
    assert result.imported == [artifact]
    [(updated,)] = rows(served, "SELECT id FROM store_relationships WHERE retired_revision IS NULL")
    assert updated != "old-id"
    assert rows(
        served, "SELECT relationship_id,successor_id FROM store_relationship_history WHERE action='supersede'",
    ) == [("old-id", updated)]
    assert stored_body(served, ALPHA) == first
    assert stored_body(served, BETA) == second
    assert not projection.run_pass().deferred
    # An edit based on the previous projection cannot overwrite the new rows.
    rel_file.write_bytes(original)
    assert watcher.import_paths([artifact]).held == [artifact]
    alpha = served / ALPHA
    alpha.write_bytes(alpha.read_bytes().replace(b"Stored relationships", b"Changed relationships"))
    assert watcher.import_paths([ALPHA]).held == [ALPHA]
    assert stored_body(served, ALPHA) == first
