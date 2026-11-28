"""The store commits relationship history and projection work with its page changes."""

import hashlib
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
    commands, find, held_operation, observe_memory, reserved_paths, semantic_index, semantic_writes,
    unit_corrections, unit_edges,
    vault_portability,
)
from exomem import vault as vault_module
from exomem.cli_ops import OpError
from exomem.governance.principal import ClientBinding, OriginSessionBinding, library_scope, request_scope
from exomem.vault_store import held, importer, projector, relationship_files, relationships, write
from exomem.vault_store import read as store_read


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
    profile = {
        "version": 1,
        "default_denied_scope_ids": [source_scope, withheld, residual],
        "clients": [{"issuer": client.issuer, "client_id": client.client_id,
                     "denied_scope_ids": [withheld, residual], "writable_scope_ids": [source_scope]}],
    }
    with monkeypatch.context() as host, request_scope(external_editor):
        host.setenv("EXOMEM_CONNECTOR_BOUNDARY_CONFIG", str(config))
        target_update = {key: value for key, value in update.items() if key != "unit"}
        target_update.update(unit_ref=claim.unit_ref, expected_fingerprint=claim.fingerprint)
        rule = scopes.parent / "rules/secret-external.yaml"
        original = rule.read_text()
        for ceiling in (0, 5):
            rule.write_text(original.replace("ceiling: 0", f"ceiling: {ceiling}"))
            profile["clients"][0]["denied_scope_ids"] = [residual] if ceiling == 5 else [withheld, residual]
            config.write_text(json.dumps(profile))
            if ceiling == 5:
                from exomem.governance import egress

                assert egress.release_level_for(served, target_path, record=False) == 5
            with pytest.raises(ValueError, match="RELATIONSHIP_END_NOT_FOUND") as hidden:
                commands.op_observe_memory(served, **target_update)
            with pytest.raises(ValueError, match="RELATIONSHIP_END_NOT_FOUND") as missing:
                commands.op_observe_memory(served, **(target_update | {
                    "relations": [{"kind": "supports", "target": "[[Beta Report#missing]]"}],
                }))
            assert str(hidden.value) == str(missing.value)
        rule.write_text(original)
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
    [(source_id,)] = rows(served, "SELECT id FROM store_unit_ids WHERE parent_path=?", source_path)
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Link to the excerpt as a page.",
        relationship_change=relationships.Change(
            actor="owner",
            added=(relationships.Relationship(
                "page-link", source_id, "supports", None, target_path,
                "exomem://memory/04e56772-21a9-44ff-b0df-0a51ed0e166b",
            ),),
        ),
    ))
    rule = served / "Knowledge Base/_Governance/rules/secret-external.yaml"
    rule.write_text(rule.read_text().replace("ceiling: 0", "ceiling: 5"))
    with request_scope(EXTERNAL):
        excerpt_view = unit_edges.relationship_page(served, source_path, "tide")
    assert excerpt_view.total == 1
    assert [(edge.target_path, edge.target) for edge in excerpt_view.edges] == [(target_path, None)]


def test_relation_file_carries_locators_and_keeps_managed_block_out_of_page_bytes():
    page = b"\xef\xbb\xbf---\r\ntitle: Tide\r\n---\r\n\r\n# Tide\r\n\r\nThe tide is high."
    projected = relationship_files.splice_block(page, ["### Stored relationships", "- Outgoing: 1"])
    assert relationship_files.strip_block(projected) == page
    bare_cr = b"# Tide\r"
    assert relationship_files.strip_block(relationship_files.splice_block(bare_cr, ["Tide"])) == bare_cr
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
    with pytest.raises(OpError, match="invalid canonical page digest"):
        relationship_files.parse(relationship_files.lines(ALPHA, 12, (), (), canonical_sha256="z" * 64))
    path = relationship_files.path_for(ALPHA)
    assert reserved_paths.classify_logical(path).blocked
    assert vault_portability.classify_artifact(path).artifact_class is vault_portability.ArtifactClass.CANONICAL


def test_supersession_is_stored_once_and_updates_current_history(served, writer):  # noqa: F811
    page = "Knowledge Base/Notes/Observations/Tide.md"
    original = (
        b"---\ntitle: Tide\ntype: insight\nstatus: active\n"
        b"exomem_id: 03b68654-8da6-433f-82fa-b8239668e43a\n---\n\n# Tide\n\n"
        b"## Claim\n- category: rule\n- id: tide\n\nThe ferry arrives at nine.\n\n"
        b"## Claim\n- category: rule\n- id: dock-old\n\nThe ferry uses the west dock.\n\n"
        b"## Claim\n- category: rule\n- id: dock-current\n- relations: supersedes: [[#^dock-old]]\n\n"
        b"The ferry uses the east dock.\n"
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
        with store_read.request_snapshot(served):
            find.recall_resolver_snapshot(served)
            corrections = unit_corrections.discover(served, [prior.unit_ref], accept=lambda _row: True)
    assert result["mutated"]
    assert original.split(b"# Tide")[1] in stored_body(served, page)
    assert stored_body(served, page).count(b"- relations: supersedes:") == 1
    [(relationship_id,)] = rows(served, "SELECT id FROM store_relationships WHERE retired_revision IS NULL")
    assert rows(served, "SELECT relationship_id,action,reason FROM store_relationship_history") == [
        (relationship_id, "add", "The ferry timetable changed."),
    ]
    previous, dock_old, dock_current, successor = units
    assert previous["superseded_by"] == [successor["id"]]
    assert successor["supersedes"] == [previous["id"]]
    assert dock_old["superseded_by"] == [dock_current["id"]]
    assert dock_current["supersedes"] == [dock_old["id"]]
    assert corrections["status"] == "available"
    assert [item["unit_ref"] for item in corrections["units"]] == [result["unit_ref"]]
    revision = read["revision_history"]["revisions"][0]
    assert revision["reason"] == "The ferry timetable changed."
    assert {item["unit_ref"] for item in revision["affected_units"]} == {
        prior.unit_ref, result["unit_ref"],
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
    candidate = semantic_index.selected_parent_index_state(served, page).document.resolve_fragment("candidate").unit
    expected = observe_memory.unit_id(candidate)
    with pytest.raises(ValueError, match="SUPERSEDE_TARGET_ALREADY_SUPERSEDED") as error:
        commands.op_observe_memory(served, **correction, transition_token=checked["semantic"]["transition_token"])
    assert expected in str(error.value)
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
    count = 550
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
    assert rows(
        served, "SELECT edge_key FROM graph_edges WHERE source_path=? AND edge_key LIKE 'stored:%' ORDER BY edge_key",
        ALPHA,
    ) == [(f"stored:edge-{anchor}",) for anchor in anchors]
    with library_scope():
        first = unit_edges.relationship_page(served, ALPHA, "tide", limit=50)
        second = unit_edges.relationship_page(served, ALPHA, "tide", limit=50, after=first.next_after)
        tail = unit_edges.relationship_page(
            served, ALPHA, "tide", after=("supports", f"edge-{anchors[-2]}"),
        )
    assert first.total == second.total == tail.total == count
    assert [edge.relationship.id for edge in first.edges + second.edges] == [
        f"edge-{anchor}" for anchor in anchors[:100]
    ]
    assert first.next_after is not None and second.next_after is not None
    assert [edge.relationship.id for edge in tail.edges] == [f"edge-{anchors[-1]}"]
    assert tail.next_after is None
    assert not projector.Projector(served, run=lambda work: work(writer)).run_pass().deferred
    projected = (served / ALPHA).read_bytes()
    assert relationship_files.strip_block(projected) == source
    assert str(count).encode() in projected
    assert len(projected) - len(source) < relationship_files.MAX_BLOCK_BYTES
    assert sum(anchor.encode() in projected for anchor in anchors) <= 12
    with (served / relationship_files.path_for(ALPHA)).open("rb") as encoded:
        portable = relationship_files.parse(encoded)
    assert {edge.id for edge in portable.relationships} == {f"edge-{anchor}" for anchor in anchors}
    other_paths = [f"Knowledge Base/Notes/Related {number}.md" for number in range(24)]
    related_anchors = tuple(f"tide-{number:02d}" for number in range(len(other_paths)))
    page_edges = tuple(
        relationships.Relationship(f"page-edge-{anchor}", anchor, "supports", None, path)
        for anchor, path in zip(related_anchors, other_paths, strict=True)
    )
    extra_units = "".join(f"- [rule] Check a tide station. ^{anchor}\n" for anchor in related_anchors).encode()
    graph_queries = []
    writer.connection.set_trace_callback(
        lambda sql: graph_queries.append(sql) if
        "FROM store_relationships r LEFT JOIN store_unit_ids t ON t.id=r.target_unit_id" in sql else None
    )
    try:
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "Populate related pages.",
            puts=(write.PagePut(ALPHA, source + extra_units),
                  *(write.PagePut(path, b"# Related") for path in other_paths)),
            relationship_change=relationships.Change(
                actor="owner", added=page_edges,
                identities=tuple(relationships.UnitIdentity(
                    anchor, ALPHA, anchor, f"exomem://memory/{source_id}",
                ) for anchor in related_anchors),
            ),
        ))
    finally:
        writer.connection.set_trace_callback(None)
    # There are more than 500 edges but fewer than 500 sources. Reads follow edges by window.
    assert 0 < len(graph_queries) <= 3
    assert not projector.Projector(served, run=lambda work: work(writer)).run_pass().deferred
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Move the related pages.",
        moves=tuple(write.PageMove(path, path.replace("Related ", "Moved ")) for path in other_paths),
    ))
    assert rows(served, "SELECT COUNT(*) FROM store_projection_queue WHERE operation='move'") == [
        (len(other_paths),),
    ]
    edge_ids = [*(edge.id for edge in page_edges), *(f"edge-{anchor}" for anchor in anchors)]
    retirement_queries = []
    for to_retire in (edge_ids[:1], edge_ids[1:]):
        statements: list[str] = []
        writer.connection.set_trace_callback(statements.append)
        try:
            write.commit(writer, write.StoreChange(
                write.Origin.GOVERNED, "Retire the old links.",
                relationship_change=relationships.Change(
                    actor="owner",
                    retired=tuple(relationships.Retirement(edge_id) for edge_id in to_retire),
                ),
            ))
        finally:
            writer.connection.set_trace_callback(None)
        begin = statements.index("BEGIN IMMEDIATE")
        retirement_queries.append(statements.index("COMMIT", begin) - begin)
    assert retirement_queries[0] == retirement_queries[1] > 0
    assert rows(served, "SELECT COUNT(*) FROM store_relationship_history WHERE action='retire'") == [(len(edge_ids),)]


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
            added=(relationships.Relationship("link-id", "tide-id", "supports", "lamp-id"),
                   relationships.Relationship("page-target", "tide-id", "supports", target_page_path=BETA)),
        ),
    ))
    projection = projector.Projector(served, run=lambda work: work(writer))
    assert not projection.run_pass().deferred
    alpha = served / relationship_files.path_for(ALPHA)
    beta = served / relationship_files.path_for(BETA)
    assert beta.exists()
    with alpha.open("rb") as encoded:
        assert relationship_files.parse(encoded).target_units[0].parent_path == BETA

    graph_rows = "SELECT dst_key FROM graph_edges WHERE edge_key='stored:link-id'"
    landings = rows(served, graph_rows)
    assert len(landings) == 1
    for body, expected in ((second.replace(b" ^lamp", b""), []), (second, landings)):
        write.commit(writer, write.StoreChange(
            write.Origin.GOVERNED, "Edit the target observation.", puts=(write.PagePut(BETA, body),),
        ))
        assert rows(served, graph_rows) == expected

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
    assert rows(served, "SELECT dst_page_key FROM graph_edges WHERE edge_key='stored:page-target'") == [
        (f"file:{moved}",),
    ]


def test_moving_a_page_only_target_before_projection_keeps_its_witness_until_retirement(
    served, writer,  # noqa: F811
):
    target = stored_body(served, BETA)
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Connect the pages.",
        puts=(write.PagePut(ALPHA, b"# Alpha\n\n- [rule] Check the tide. ^tide\n"),),
        relationship_change=relationships.Change(
            actor="owner",
            identities=(relationships.UnitIdentity("tide-id", ALPHA, "tide"),),
            added=(relationships.Relationship("page-edge", "tide-id", "supports", target_page_path=BETA),),
        ),
    ))
    assert not (served / relationship_files.path_for(BETA)).exists()
    moved = "Knowledge Base/Notes/Lantern.md"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Move the target page.", moves=(write.PageMove(BETA, moved),),
    ))
    projection = projector.Projector(served, run=lambda work: work(writer))
    assert not projection.run_pass().deferred
    moved_page = served / moved
    artifact = served / relationship_files.path_for(moved)
    assert b"Incoming supports for page: 1" in moved_page.read_bytes()
    assert relationship_files.strip_block(moved_page.read_bytes()) == target
    with artifact.open("rb") as encoded:
        witness = relationship_files.parse(encoded)
    assert witness.canonical_sha256 == hashlib.sha256(target).hexdigest()
    assert witness.units == () and witness.relationships == ()

    edited = target + b"\nCheck the notebook tomorrow.\n"
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Edit the target page.", puts=(write.PagePut(moved, edited),),
    ))
    assert not projection.run_pass().deferred
    with artifact.open("rb") as encoded:
        assert relationship_files.parse(encoded).canonical_sha256 == hashlib.sha256(edited).hexdigest()

    relation_path = relationship_files.path_for(moved)
    artifact.unlink()
    watcher = importer.Importer(served, run=lambda work: work(writer))
    assert watcher.import_paths([relation_path]).held == [relation_path]
    with library_scope():
        [item] = held.list_held(served)
        held.resolve_held(served, relation_path, "keep-store", why="Restore the relation witness.",
                          seen=item.item_version, writer=writer)
    assert not projection.run_pass().deferred
    assert not projection.run_pass().deferred
    with artifact.open("rb") as encoded:
        assert relationship_files.parse(encoded).canonical_sha256 == hashlib.sha256(edited).hexdigest()

    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Retire the relationship.",
        relationship_change=relationships.Change(actor="owner", retired=(relationships.Retirement("page-edge"),)),
    ))
    assert not projection.run_pass().deferred
    assert not projection.run_pass().deferred
    assert moved_page.read_bytes() == edited
    assert not artifact.exists()
    with (served / relationship_files.path_for(ALPHA)).open("rb") as encoded:
        inactive = relationship_files.parse(encoded)
    assert inactive.canonical_sha256 is None
    assert [unit.id for unit in inactive.units] == ["tide-id"]


def test_a_rename_into_a_vacant_path_preserves_both_old_and_current_targets(served, writer):  # noqa: F811
    from exomem import audit, memory_refs

    other = "Knowledge Base/Notes/Lantern.md"
    alpha_id = "e8f840b6-3cf8-44cf-ab95-12d05683a194"
    beta_id = "04e56772-21a9-44ff-b0df-0a51ed0e166b"
    other_id = "05e56772-21a9-44ff-b0df-0a51ed0e166b"
    source = f"---\nexomem_id: {alpha_id}\n---\n\n- [rule] Check the tide. ^tide\n".encode()
    original = f"---\nexomem_id: {beta_id}\n---\n\n- [rule] Light the lamp. ^lamp\n".encode()
    replacement = original.replace(beta_id.encode(), other_id.encode())
    alpha_ref, beta_ref, other_ref = map(memory_refs.memory_ref, (alpha_id, beta_id, other_id))
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record two different lamps.",
        puts=(write.PagePut(ALPHA, source), write.PagePut(BETA, original),
              write.PagePut(other, replacement)),
        relationship_change=relationships.Change(
            actor="owner",
            identities=(
                relationships.UnitIdentity("source", ALPHA, "tide", alpha_ref),
                relationships.UnitIdentity("old-lamp", BETA, "lamp", beta_ref),
                relationships.UnitIdentity("new-lamp", other, "lamp", other_ref),
            ),
            added=(
                relationships.Relationship("old-page", "source", "supports", None, BETA, beta_ref),
                relationships.Relationship("new-page", "source", "supports", None, other, other_ref),
                relationships.Relationship("old-unit", "source", "supports", "old-lamp"),
                relationships.Relationship("new-unit", "source", "supports", "new-lamp"),
            ),
        ),
    ))
    projection = projector.Projector(served, run=lambda work: work(writer))
    assert not projection.run_pass().deferred
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Remove the obsolete page.", deletes=(write.PageDelete(BETA),),
    ))
    assert not projection.run_pass().deferred
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Move the remaining page.", moves=(write.PageMove(other, BETA),),
    ))
    assert not projection.run_pass().deferred
    assert stored_body(served, BETA) == replacement
    with library_scope():
        page = unit_edges.relationship_page(served, ALPHA, "tide")
        orphaned = audit.audit(served, categories=[audit.RELATIONSHIP_ORPHAN]).findings
    assert page.total == 2
    assert {edge.relationship.id for edge in page.edges} == {"new-unit", "new-page"}
    assert {(item.path, item.meta.get("unit_id"), item.meta.get("page_ref"))
            for item in orphaned} == {(BETA, "old-lamp", None), (BETA, None, beta_ref)}
    with (served / relationship_files.path_for(ALPHA)).open("rb") as encoded:
        portable = relationship_files.parse(encoded)
    assert {(end.parent_path, end.anchor, end.parent_ref) for end in portable.target_units} == {
        (BETA, "lamp", beta_ref), (BETA, "lamp", other_ref),
    }


def test_relationships_can_read_endpoints_on_a_read_only_reference(served, writer):  # noqa: F811
    from exomem import audit

    manual = "Reference/Manual.md"
    source_path = "Knowledge Base/Notes/Observations/Tide.md"
    companion_path = "Knowledge Base/Notes/Observations/Companion.md"
    (served / manual).parent.mkdir()
    source_id = "e8f840b6-3cf8-44cf-ab95-12d05683a194"
    source = (
        f"---\ntitle: Tide\ntype: insight\nstatus: active\nexomem_id: {source_id}\n---\n\n"
        "See [[Companion]].\n\n- [rule] Check the tide. ^tide\n"
    ).encode()
    companion = b"---\ntitle: Companion\ntype: insight\nstatus: active\n---\n\nRecord tide changes.\n"
    original = b"---\ntitle: Manual\n---\n\n- [rule] Light the lamp. ^lamp\n"
    (served / manual).write_bytes(original)
    imported = importer.Importer(served, run=lambda work: work(writer)).import_paths([manual])
    assert imported.refreshed == [manual]
    write.commit(writer, write.StoreChange(
        write.Origin.GOVERNED, "Record an observation.",
        puts=(write.PagePut(source_path, source), write.PagePut(companion_path, companion)),
    ))
    claim = semantic_index.selected_parent_index_state(served, source_path).document.units[0]
    result = commands.op_observe_memory(
        served, path=source_path, operation="assess", unit=observe_memory.unit_id(claim),
        relations=[
            {"kind": "supports", "target": "[[Reference/Manual]]"},
            {"kind": "supports", "target": "[[Reference/Manual#^lamp]]"},
        ],
        reason="Connect to the reference.",
    )
    assert result["mutated"]
    assert not projector.Projector(served, run=lambda work: work(writer)).run_pass().deferred
    assert (served / manual).read_bytes() == original
    with library_scope():
        linked = unit_edges.relationship_page(served, source_path, "tide")
        report = audit.audit(served, categories=[audit.RELATIONSHIP_ORPHAN])
    assert {(edge.target_path, edge.target.anchor if edge.target else None) for edge in linked.edges} == {
        (manual, None), (manual, "lamp"),
    }
    assert not report.findings
    (served / manual).unlink()
    with library_scope():
        assert unit_edges.relationship_page(served, source_path, "tide").total == 0
        report = audit.audit(served, categories=[audit.RELATIONSHIP_ORPHAN])
    assert {(item.path, item.meta.get("anchor")) for item in report.findings} == {
        (manual, None), (manual, "lamp"),
    }
