"""The store commits relationship history and projection work with its page changes."""

import json
import sqlite3
from dataclasses import replace

import pytest
from test_managed_collections_migration import coordinator_url  # noqa: F401
from test_vault_store_migration import ALPHA, BETA, vault  # noqa: F401
from test_vault_store_read_write import rows, stored, stored_body, writer  # noqa: F401
from test_vault_store_readers import EXTERNAL, restrict_external
from test_vault_store_write_path import served  # noqa: F401

from exomem import (
    commands, held_operation, observe_memory, reserved_paths, semantic_index, semantic_writes, unit_edges,
    vault_portability,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance.principal import ClientBinding, OriginSessionBinding, library_scope, request_scope
from exomem.vault_store import held, importer, projector, relationship_files, relationships, write


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


def test_assess_anchors_the_far_end_and_later_changes_only_relationships(
    served, writer, tmp_path, monkeypatch,  # noqa: F811
):
    source_path = "Knowledge Base/Notes/Observations/Alpha Report.md"
    target_path = "Knowledge Base/Notes/Observations/Beta Report.md"
    alpha = (
        "---\ntitle: Alpha Report\ntype: insight\nstatus: active\n"
        "exomem_id: e8f840b6-3cf8-44cf-ab95-12d05683a194\nupdated: 2026-07-15\n---\n\n"
        "# Alpha\n\n## Claim\n- category: rule\n- id: tide\n\nCheck the tide.\n"
    ).replace("\n", "\r\n").encode()
    beta = (
        "---\ntitle: Beta Report\ntype: insight\nstatus: active\n"
        "exomem_id: 04e56772-21a9-44ff-b0df-0a51ed0e166b\ntags: [secret]\n---\n\n"
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
    restrict_external(served, 'tags: ["secret"]')
    scopes = served / "Knowledge Base/_Governance/scopes"
    source_scope = "01ARZ3NDEKTSV4RRFFQ69G5FBE"
    residual = "01ARZ3NDEKTSV4RRFFQ69G5FBF"
    withheld = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    (scopes / "source.yaml").write_text(
        f'governance_version: 1\nid: {source_scope}\npaths: ["Notes/Observations/Alpha Report.md"]\n',
    )
    (scopes / "residual.yaml").write_text(f"governance_version: 1\nid: {residual}\nresidual: true\n")
    client = ClientBinding("https://memory.example.test", "external-editor")
    external_editor = replace(
        EXTERNAL, client_binding=client, origin_session=OriginSessionBinding("fixture", "1", EXTERNAL.audience_id),
    )
    config = tmp_path / "host-boundary.json"
    config.write_text(json.dumps({
        "version": 1,
        "default_denied_scope_ids": [source_scope, withheld, residual],
        "clients": [{"issuer": client.issuer, "client_id": client.client_id,
                     "denied_scope_ids": [withheld, residual], "writable_scope_ids": [source_scope]}],
    }))
    with monkeypatch.context() as host, request_scope(external_editor):
        host.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
        target_update = {key: value for key, value in update.items() if key != "unit"}
        target_update.update(unit_ref=claim.unit_ref, expected_fingerprint=claim.fingerprint)
        with pytest.raises(ValueError, match="RELATIONSHIP_END_NOT_FOUND") as hidden:
            commands.op_observe_memory(served, **target_update)
        with pytest.raises(ValueError, match="RELATIONSHIP_END_NOT_FOUND") as missing:
            commands.op_observe_memory(
                served, **(target_update | {"relations": [{"kind": "supports", "target": "[[No report]]"}]}),
            )
        assert str(hidden.value) == str(missing.value)
    (scopes / "source.yaml").unlink()
    (scopes / "residual.yaml").unlink()
    policy = served / "Knowledge Base/_access.yaml"
    policy.write_text("readonly:\n  - Notes/Observations/Beta Report.md\n")
    with pytest.raises(ValueError, match="RELATIONSHIP_END_NOT_WRITABLE"):
        commands.op_observe_memory(served, **update)
    policy.unlink()
    assert stored_body(served, source_path) == alpha and stored_body(served, target_path) == beta
    assert not rows(served, "SELECT id FROM store_relationships")
    preview = commands.op_observe_memory(served, **update, validate_only=True)
    assert not preview["mutated"] and stored_body(served, target_path) == beta
    commands.op_observe_memory(served, **update, transition_token=preview["semantic"]["transition_token"])

    assert stored_body(served, source_path) == alpha
    [(anchor,)] = rows(served, "SELECT anchor FROM store_unit_ids WHERE parent_path = ?", target_path)
    assert stored_body(served, target_path) == beta.replace(b"Light the lamp.", f"Light the lamp. ^{anchor}".encode())
    assert rows(served, "SELECT reason FROM store_relationship_history") == [("Record the support.",)]
    page_revisions = "SELECT path,revision,sha256 FROM store_pages WHERE path IN (?,?) ORDER BY path"
    before = rows(served, page_revisions, source_path, target_path)
    contradiction = dict(
        path=source_path, operation="assess", unit=claim_id,
        relations=[{"kind": "contradicts", "target": f"[[Beta Report#{anchor}]]"}],
        reason="Record the conflicting observation.",
    )
    checked = commands.op_observe_memory(served, **contradiction, validate_only=True)
    token = checked["semantic"]["transition_token"]
    assert commands.op_observe_memory(served, **contradiction, transition_token=token)["mutated"]
    revision = rows(served, "SELECT revision FROM store_mode")
    assert not commands.op_observe_memory(served, **contradiction, transition_token=token)["mutated"]
    assert rows(served, "SELECT revision FROM store_mode") == revision
    after = rows(served, page_revisions, source_path, target_path)
    assert before == after
    assert rows(served, "SELECT relation_type FROM store_relationships ORDER BY relation_type") == [
        ("contradicts",), ("supports",),
    ]
    with library_scope():
        page = unit_edges.relationship_page(served, source_path, "tide", limit=1)
        remaining = unit_edges.relationship_page(served, source_path, "tide", limit=1, after=page.next_after)
        public = commands.op_read_memory(
            served, path=source_path, unit_ref=claim.unit_ref, relationship_limit=1,
        )["stored_relationships"]
    assert page.total == remaining.total == 2
    assert page.next_after is not None and remaining.next_after is None
    assert [edge.relationship.relation_type for edge in (*page.edges, *remaining.edges)] == [
        "contradicts", "supports",
    ]
    assert public == page.as_dict()
    with request_scope(EXTERNAL):
        private = commands.op_read_memory(
            served, path=source_path, unit_ref=claim.unit_ref, relationship_limit=1,
        )["stored_relationships"]
    assert private["total"] == 0 and private["items"] == []
    assert rows(
        served, "SELECT COUNT(*) FROM graph_edges WHERE edge_key LIKE 'stored:%' AND source_path=?", source_path,
    ) == [(2,)]
    commands.op_observe_memory(
        served, path=source_path, operation="update", unit=claim_id, category="rule", kind="claim",
        content="Check the tide.", relations=[
            {"kind": "supports", "target": f"[[Beta Report#{anchor}]]"},
        ], reason="Remove the conflicting relationship.",
    )
    assert rows(served, page_revisions, source_path, target_path) == before
    with pytest.raises(ValueError, match="LIFECYCLE_TRANSITION_MISMATCH"):
        commands.op_observe_memory(served, **contradiction, transition_token=token)
    assert rows(served, "SELECT relation_type FROM store_relationships WHERE retired_revision IS NULL") == [
        ("supports",),
    ]


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


def test_supersession_is_stored_once_and_updates_current_history(served, writer):  # noqa: F811
    page = "Knowledge Base/Notes/Observations/Tide.md"
    original = (
        b"---\ntitle: Tide\ntype: insight\nstatus: active\n"
        b"exomem_id: 03b68654-8da6-433f-82fa-b8239668e43a\n---\n\n# Tide\n\n"
        b"## Claim\n- category: rule\n- id: tide\n\nThe ferry arrives at nine.\n"
    )
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the timetable.", puts=(write.PagePut(page, original),),
    ))
    prior = semantic_index.selected_parent_index_state(served, page).document.units[0]
    with library_scope():
        result = commands.op_observe_memory(
            served, path=page, operation="supersede", unit=observe_memory.unit_id(prior),
            content="The ferry arrives at ten.", reason="The ferry timetable changed.",
        )
        read = commands.op_read_memory(served, path=page, include_history=True)
        units = observe_memory.page_units(served, read)
    assert result["mutated"]
    assert original.split(b"# Tide")[1] in stored_body(served, page)
    assert b"supersedes::" not in stored_body(served, page)
    [(relationship_id,)] = rows(served, "SELECT id FROM store_relationships WHERE retired_revision IS NULL")
    assert rows(served, "SELECT relationship_id,action,reason FROM store_relationship_history") == [
        (relationship_id, "add", "The ferry timetable changed."),
    ]
    previous, successor = units
    assert previous["superseded_by"] == [successor["id"]]
    assert successor["supersedes"] == [previous["id"]]
    revision = read["revision_history"]["revisions"][0]
    assert revision["reason"] == "The ferry timetable changed."
    assert {item["unit_ref"] for item in revision["affected_units"]} == {
        unit.unit_ref for unit in semantic_index.selected_parent_index_state(served, page).document.units
    }
    with pytest.raises(ValueError, match="ALREADY_SUPERSEDED"):
        commands.op_observe_memory(
            served, path=page, operation="supersede", unit=previous["id"],
            content="The ferry arrives at eleven.", reason="Correct the timetable again.",
        )


def test_supersede_refuses_a_relationship_that_lands_after_preflight(
    served, writer, monkeypatch,  # noqa: F811
):
    page = "Knowledge Base/Notes/Observations/Tide.md"
    original = (
        b"---\ntitle: Tide\ntype: insight\nstatus: active\nupdated: 2026-07-15\n"
        b"exomem_id: 03b68654-8da6-433f-82fa-b8239668e43a\n---\n\n# Tide\n\n"
        b"## Claim\n- category: rule\n- id: tide\n\nThe ferry arrives at nine.\n\n"
        b"## Claim\n- category: rule\n- id: candidate\n\nThe ferry arrives at ten.\n"
    )
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the timetable.", puts=(write.PagePut(page, original),),
    ))
    assessment = dict(path=page, operation="assess", unit="candidate", reason="Record the corrected time.")
    commands.op_observe_memory(
        served, **assessment, relations=[{"kind": "supports", "target": "[[#tide]]"}],
    )
    correction = dict(path=page, operation="supersede", unit="tide", content="The ferry arrives at eleven.")
    checked = commands.op_observe_memory(served, **correction, validate_only=True)
    preflight = semantic_writes.preflight_existing
    landed = False

    def let_assessment_land(*args, **kwargs):
        nonlocal landed
        prepared = preflight(*args, **kwargs)
        if not landed:
            landed = True
            commands.op_observe_memory(
                served, **assessment, relations=[{"kind": "supersedes", "target": "[[#tide]]"}],
            )
        return prepared

    monkeypatch.setattr(semantic_writes, "preflight_existing", let_assessment_land)
    with pytest.raises(ValueError, match="SUPERSEDE_TARGET_ALREADY_SUPERSEDED"):
        commands.op_observe_memory(served, **correction, transition_token=checked["semantic"]["transition_token"])
    assert landed and stored_body(served, page) == original
    assert rows(served, "SELECT relation_type FROM store_relationships ORDER BY relation_type") == [
        ("supersedes",), ("supports",),
    ]


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
    changed = alpha.read_bytes().replace(b"Stored relationships", b"Changed relationships")
    alpha.write_bytes(changed.replace(b"Check the tide.", b"Check the evening tide."))
    assert watcher.import_paths([ALPHA]).held == [ALPHA]
    assert stored_body(served, ALPHA) == first
    with request_scope(EXTERNAL):
        assert held_operation.listing(served)["items"] == []
    with library_scope():
        items = {item.path: item for item in held.list_held(served)}
        held.resolve_held(served, artifact, "keep-store", why="Keep the newer relationship.",
                          seen=items[artifact].item_version, writer=writer)
        held.resolve_held(served, ALPHA, "keep-mine", why="Keep the edited observation.",
                          seen=items[ALPHA].item_version, writer=writer)
    assert stored_body(served, ALPHA) == first.replace(b"Check the tide.", b"Check the evening tide.")
    assert not projection.run_pass().deferred
    assert rows(served, "SELECT path FROM store_held") == []


def test_a_relationship_hub_has_bounded_reads_and_a_complete_portable_file(served, writer):  # noqa: F811
    count = 500
    source_id = "e8f840b6-3cf8-44cf-ab95-12d05683a194"
    target_id = "04e56772-21a9-44ff-b0df-0a51ed0e166b"
    source = (f"---\nexomem_id: {source_id}\n---\n# Alpha\n\n- [rule] Check the tide. ^tide\n").encode()
    anchors = [f"lamp-{number:03d}" for number in range(count)]
    target = (f"---\nexomem_id: {target_id}\n---\n# Beta\n\n"
              + "".join(f"- [rule] Light lamp {anchor}. ^{anchor}\n" for anchor in anchors)).encode()
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Connect related rules.",
        puts=(write.PagePut(ALPHA, source), write.PagePut(BETA, target)),
        relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide", f"exomem://memory/{source_id}"),
                        *(relationships.UnitIdentity(anchor, BETA, anchor, f"exomem://memory/{target_id}")
                          for anchor in anchors)),
            added=tuple(relationships.Relationship(f"edge-{anchor}", "tide-id", "supports", anchor)
                        for anchor in anchors),
        ),
    ))
    with library_scope():
        first = unit_edges.relationship_page(served, ALPHA, "tide", limit=50)
        second = unit_edges.relationship_page(served, ALPHA, "tide", limit=50, after=first.next_after)
    assert first.total == second.total == count
    assert [edge.relationship.id for edge in first.edges + second.edges] == [
        f"edge-{anchor}" for anchor in anchors[:100]
    ]
    assert first.next_after is not None and second.next_after is not None
    assert not projector.Projector(served, run=lambda work: work(writer)).run_pass().deferred
    projected = (served / ALPHA).read_bytes()
    assert relationship_files.strip_block(projected) == source
    assert str(count).encode() in projected
    assert len(projected) - len(source) < relationship_files.MAX_BLOCK_BYTES
    assert sum(anchor.encode() in projected for anchor in anchors) <= 12
    with (served / relationship_files.path_for(ALPHA)).open("rb") as encoded:
        portable = relationship_files.parse(encoded)
    assert {edge.id for edge in portable.relationships} == {f"edge-{anchor}" for anchor in anchors}


def test_moving_a_target_refreshes_other_pages_relations_files(served, writer):  # noqa: F811
    first = b"# Alpha\n\n- [rule] Check the tide. ^tide\n"
    second = b"# Beta\n\n- [rule] Light the lamp. ^lamp\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record the rules.",
        puts=(write.PagePut(ALPHA, first), write.PagePut(BETA, second)),
        relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),
                        relationships.UnitIdentity("lamp-id", BETA, "lamp")),
            added=(relationships.Relationship("link-id", "tide-id", "supports", "lamp-id"),),
        ),
    ))
    projection = projector.Projector(served, run=lambda work: work(writer))
    assert not projection.run_pass().deferred
    alpha = served / relationship_files.path_for(ALPHA)
    beta = served / relationship_files.path_for(BETA)
    assert beta.exists()
    with alpha.open("rb") as encoded:
        assert relationship_files.parse(encoded).target_units[0].parent_path == BETA

    moved = "Knowledge Base/Notes/Lantern.md"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Move the observation.", moves=(write.PageMove(BETA, moved),),
    ))
    assert not projection.run_pass().deferred
    with alpha.open("rb") as encoded:
        relations = relationship_files.parse(encoded)
    assert relations.target_units[0].parent_path == moved
    assert relations.target_units[0].id == "lamp-id" and relations.relationships[0].id == "link-id"
    assert not beta.exists() and (served / relationship_files.path_for(moved)).exists()
