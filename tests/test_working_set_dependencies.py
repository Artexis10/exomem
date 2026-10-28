"""A selected claim must keep its authored operating dependency actionable."""

import json
import os
from pathlib import Path

import pytest
import test_compiled_history as history_fixtures
import test_working_set_material as material_fixtures

from exomem import (
    commands,
    epistemic_graph,
    find,
    freshness,
    graph_sync,
    lexstore,
    semantic_index,
    working_set_index,
    working_set_runtime,
)
from exomem import (
    vault as vault_module,
)

material_vault = material_fixtures.material_vault
owner = history_fixtures.owner


def _create(root: Path, title: str, body: str, note_type: str = "insight") -> str:
    args = dict(content=body, note_type=note_type, title=title, projects=["harbor-study"])
    preview = commands.op_note(root, validate_only=True, **args)
    review = {}
    if preview.get("reviewed_none_required"):
        review = dict(
            relation_disposition="reviewed_none",
            relation_review_hash=preview["draft_hash"],
            relation_review_reason="No further honest relation exists in this invented fixture.",
        )
    result = commands.op_note(
        root, **args, **review,
        draft_id=preview["draft_id"], draft_hash=preview["draft_hash"],
        draft_token=preview["draft_token"],
    )
    return result["path"]


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
@pytest.mark.parametrize("placement", ["same_page", "cross_page"])
def test_activation_keeps_an_authored_exact_dependency_actionable(
    material_vault: Path, owner, placement: str,
) -> None:
    root = material_vault
    limitation = "Operation is conditional on the seal passing its pressure check."
    target_body = (
        "## Claim\n- category: observation\n- id: limitation\n\n"
        f"{limitation}\n\n"
        "## Claim\n- category: observation\n- id: unrelated\n\n"
        "Inventory labels are blue.\n"
    )
    other = _create(root, "Neutral operating envelope", target_body) if placement == "cross_page" else ""
    target = f"{other}#limitation" if other else "#limitation"
    primary_body = (
        "## Claim\n- category: observation\n- id: authorization\n"
        f"- relations: depends_on: [[{target}]]\n\n"
        "Violet worksheet calibration may proceed.\n\n"
    )
    if placement == "same_page":
        primary_body += target_body
    primary = _create(root, "Violet worksheet calibration", primary_body)
    target_parent = other or primary
    primary_unit = semantic_index.current_parent_index_state(root, primary).document.resolve_fragment("authorization").unit
    target_unit = semantic_index.current_parent_index_state(root, target_parent).document.resolve_fragment("limitation").unit
    authored = commands.op_read_memory(root, path=primary, unit_ref=primary_unit.unit_ref)
    expanded = commands.op_read_memory(root, path=target_parent, unit_ref=target_unit.unit_ref)
    assert authored["status"] == expanded["status"] == "found"
    assert any(relation["kind"] == "depends_on" for relation in authored["unit"]["relations"])
    assert expanded["unit"]["content"].strip() == limitation
    assert len(expanded["parent_context"]["markdown"]) <= 2400
    lexstore.ensure_fresh(root)
    assert graph_sync.drain_active_rebuilds()
    freshness.seed(root, "vault", ((str(path), freshness.stat_signature(path))
                                  for path in vault_module.walk_vault_md(root)))
    find.evict_resolver_caches(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    find.recall_resolver_snapshot(root, expected_checkpoint=freshness.live_recall_checkpoint(root, "vault"))
    graph = commands.op_graph_context(root, unit_ref=primary_unit.unit_ref, traversal_profile="all", depth=1)
    assert graph["available"] is True
    seed_key = next(node["node_key"] for node in graph["seeds"]
                    if node["metadata"].get("unit_ref") == primary_unit.unit_ref)
    target_key = next(node["node_key"] for node in graph["nodes"]
                      if node.get("metadata", {}).get("unit_ref") == target_unit.unit_ref)
    assert any(edge["src_key"] == seed_key and edge["dst_key"] == target_key
               and edge["relation_type"] == "depends_on" for edge in graph["edges"])
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    packet = commands.op_activate_context(
        root, turn="In harbor-study, may violet worksheet calibration proceed?", max_chars=4000,
    )
    served = {unit["ref"]: unit for unit in packet["units"]}
    assert primary_unit.unit_ref in served
    assert packet["budget"]["used_chars"] <= 4000
    assert "Inventory labels are blue." not in json.dumps(packet)
    evidence = {
        "placement": placement, "authored": authored, "dependency": expanded,
        "graph": graph, "packet": packet,
        "source_truth": {"primary": primary_unit.unit_ref, "dependency": target_unit.unit_ref,
                         "required_limitation": limitation, "unrelated_sibling": "Inventory labels are blue."},
    }
    if os.environ.get("COMPILER_PROBE_ROOT"):
        destination = Path(os.environ["COMPILER_PROBE_ROOT"]) / f"{placement}-evidence.json"
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    pointer = next(pointer for pointer in packet["pointers"] if pointer["ref"] == target_unit.unit_ref)
    assert primary_unit.unit_ref in pointer["why"]
    assert "depends_on" in pointer["why"]
    assert pointer["path"] == target_parent
    assert pointer["reason"] == "requires_read"
    followed = commands.op_read_memory(root, path=pointer["path"], unit_ref=pointer["ref"])
    assert followed["status"] == "found"
    assert followed["unit"]["content"].strip() == limitation


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
def test_deleting_an_exact_dependency_page_leaves_its_claim_partial(material_vault: Path, owner) -> None:
    root = material_vault
    dependency = _create(root, "Neutral operating envelope", (
        "## Claim\n- category: observation\n- id: limitation\n\n"
        "Operation is conditional on the seal passing its pressure check.\n"
    ))
    primary = _create(root, "Violet worksheet calibration", (
        "## Claim\n- category: observation\n- id: authorization\n"
        f"- relations: depends_on: [[{dependency}#limitation]]\n\n"
        "Violet worksheet calibration may proceed.\n"
    ))
    primary_unit = semantic_index.current_parent_index_state(root, primary).document.resolve_fragment("authorization").unit
    removed_unit = semantic_index.current_parent_index_state(root, dependency).document.resolve_fragment("limitation").unit
    lexstore.ensure_fresh(root)
    assert graph_sync.drain_active_rebuilds()
    freshness.seed(root, "vault", ((str(path), freshness.stat_signature(path))
                                  for path in vault_module.walk_vault_md(root)))
    find.evict_resolver_caches(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()

    commands.op_manage_memory_file(
        root, operation="delete", path=dependency, confirm=True, force_orphan=True, allow_curated=True,
    )
    lexstore.ensure_fresh(root)
    assert graph_sync.drain_active_rebuilds()
    find.evict_resolver_caches(root)
    find.recall_resolver_snapshot(root, expected_checkpoint=freshness.live_recall_checkpoint(root, "vault"))
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    packet = commands.op_activate_context(
        root, turn="In harbor-study, may violet worksheet calibration proceed?", max_chars=4000,
    )

    assert primary_unit.unit_ref in {unit["ref"] for unit in packet["units"]}
    assert removed_unit.unit_ref not in {pointer["ref"] for pointer in packet["pointers"]}
    assert packet["generation"]["correction_context"] == "partial"


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
@pytest.mark.parametrize("reader", ["owner", "restricted"])
def test_an_exact_dependency_through_a_shared_page_name_leaves_its_claim_partial(
    material_vault: Path, owner, reader: str,
) -> None:
    root = material_vault
    claim = "## Claim\n- category: observation\n- id: limitation\n\n{}\n"
    first = _create(root, "Neutral operating envelope",
                    claim.format("Operation is conditional on the seal passing its pressure check."))
    second = _create(root, "Neutral operating envelope",
                     claim.format("Operation needs a signed valve log."), note_type="pattern")
    assert first != second and Path(first).stem == Path(second).stem
    if reader == "restricted":
        from test_governance_egress import write_rule, write_scope
        from test_working_set_egress import _reset_governance_state

        hidden = root / "Knowledge Base/Notes/Private/seal-log.md"
        hidden.parent.mkdir(parents=True, exist_ok=True)
        hidden.write_text("---\ntype: pattern\n---\n\nA restricted maintenance log.\n")
        write_scope(root, paths="Notes/Private/**")
        write_rule(root, ceiling=0, audience="external")
        _reset_governance_state(root)
    primary = _create(root, "Violet worksheet calibration", (
        "## Claim\n- category: observation\n- id: authorization\n"
        f"- relations: depends_on: [[{Path(first).stem}#limitation]]\n\n"
        "Violet worksheet calibration may proceed.\n"
    ))
    primary_unit = semantic_index.current_parent_index_state(root, primary).document.resolve_fragment("authorization").unit
    candidates = {
        semantic_index.current_parent_index_state(root, path).document.resolve_fragment("limitation").unit.unit_ref
        for path in (first, second)
    }
    lexstore.ensure_fresh(root)
    assert graph_sync.drain_active_rebuilds()
    freshness.seed(root, "vault", ((str(path), freshness.stat_signature(path))
                                  for path in vault_module.walk_vault_md(root)))
    find.evict_resolver_caches(root)
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    find.recall_resolver_snapshot(root, expected_checkpoint=freshness.live_recall_checkpoint(root, "vault"))
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    from test_governance_egress import _external

    from exomem import unit_corrections
    from exomem.governance.principal import owner_principal, request_scope

    with request_scope(owner_principal() if reader == "owner" else _external()):
        visible = unit_corrections.reader_view(root)
        if reader == "restricted":
            assert visible is not None and visible(first) and visible(second)
            assert not visible("Knowledge Base/Notes/Private/seal-log.md")
        packet = commands.op_activate_context(
            root, turn="In harbor-study, may violet worksheet calibration proceed?", max_chars=4000,
        )

    assert primary_unit.unit_ref in {unit["ref"] for unit in packet["units"]}
    assert not candidates & {pointer["ref"] for pointer in packet["pointers"]}
    assert packet["generation"]["correction_context"] == "partial"


def _selected(root: Path, path: str, fragment: str):
    from exomem import working_set

    unit = semantic_index.current_parent_index_state(root, path).document.resolve_fragment(fragment).unit
    hit = find._semantic_unit_hit(find._CACHE.get(root / path, root), unit, bm25_rank=1, bm25_score=1.0)
    return working_set._unit_items(None, [hit])[0]


def _packet(root: Path, items, *, max_chars=4000, visible=None, roles=()):
    from exomem import lifecycle_statuses, working_set

    return working_set.build_packet(
        items=items, anchors=(), roles=roles, current_state=(), ambiguity=(), missing=(),
        max_chars=max_chars, generation={}, status="resolved", visible=visible,
        status_basis=lifecycle_statuses.Basis(root),
    )


@pytest.fixture
def exact_relations(tmp_path: Path):
    from test_relation_fragment_targets import SOURCE, SOURCE_ID, TARGET, _built, _write
    from test_unit_correction_context import _publish

    root, index = _built(tmp_path)
    _write(root, SOURCE, f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n" + "".join(
        f"## Claim\n- category: config\n- id: {fragment}\n"
        f"- relations: depends_on: [[{TARGET}#rich-1]]\n\n{body}\n\n"
        for fragment, body in (("first", "The first operation may proceed."),
                               ("second", "The second operation may proceed."))
    ))
    _publish(index)
    return root, index, _selected(root, SOURCE, "first"), _selected(root, SOURCE, "second")


def test_relation_bindings_keep_each_source_even_when_target_body_is_served(exact_relations) -> None:
    from test_relation_fragment_targets import TARGET

    root, _index, first, second = exact_relations
    target = _selected(root, TARGET, "rich-1")
    packet = _packet(root, (first, second, target))
    assert {row["ref"] for row in packet["units"]} == {first.ref, second.ref, target.ref}
    pointers = [row for row in packet["pointers"] if row["ref"] == target.ref]
    assert len(pointers) == 2
    assert all(any(source.ref in row["why"] for row in pointers) for source in (first, second))


def test_omitted_source_leaves_no_orphan_relation_pointer(exact_relations) -> None:
    from dataclasses import replace

    root, _index, first, _second = exact_relations
    packet = _packet(root, (replace(first, text="Long operating rationale. " * 50),), max_chars=500)
    assert first.ref not in {row["ref"] for row in packet["units"]}
    assert all(row["ref"] == first.ref for row in packet["pointers"])
    assert packet["budget"]["used_chars"] <= 500


def test_stable_source_reference_does_not_attach_current_relations_to_old_prose(exact_relations) -> None:
    from test_relation_fragment_targets import SOURCE
    from test_unit_correction_context import _publish

    root, index, old, _second = exact_relations
    path = root / SOURCE
    path.write_text(path.read_text().replace("The first operation may proceed.", "The first operation is suspended."))
    _publish(index)
    current = _selected(root, SOURCE, "first")
    assert old.ref == current.ref and old.provenance["source_hash"] != current.provenance["source_hash"]
    packet = _packet(root, (old,))
    assert packet["pointers"] == []
    assert packet["generation"]["correction_context"] == "partial"


def test_stale_duplicate_cannot_shadow_the_served_current_claims_relation(exact_relations) -> None:
    from dataclasses import replace

    from test_relation_fragment_targets import SOURCE, TARGET
    from test_unit_correction_context import _publish

    root, index, old, _second = exact_relations
    path = root / SOURCE
    path.write_text(path.read_text().replace("The first operation may proceed.", "The first operation uses the revised procedure."))
    _publish(index)
    current = replace(_selected(root, SOURCE, "first"), role="primary")
    old = replace(old, role="secondary")
    assert old.ref == current.ref and old.provenance["source_hash"] != current.provenance["source_hash"]
    packet = _packet(root, (old, current), roles=({"id": "primary"}, {"id": "secondary"}))
    assert [row["text"] for row in packet["units"]] == [current.text]
    target = _selected(root, TARGET, "rich-1")
    assert [row["ref"] for row in packet["pointers"]] == [target.ref]
    assert packet["pointers"][0]["role"] == "primary"


@pytest.mark.parametrize("fragment", ["missing", "dup"])
def test_missing_or_ambiguous_target_never_substitutes_another_unit(exact_relations, fragment: str) -> None:
    from test_relation_fragment_targets import DUP_TARGET, SOURCE, TARGET
    from test_unit_correction_context import _publish

    root, index, _first, _second = exact_relations
    path = root / SOURCE
    target = DUP_TARGET if fragment == "dup" else TARGET
    path.write_text(path.read_text().replace(f"{TARGET}#rich-1", f"{target}#{fragment}"))
    _publish(index)
    packet = _packet(root, (_selected(root, SOURCE, "first"),))
    assert packet["pointers"] == []
    assert packet["generation"]["correction_context"] == "partial"


@pytest.mark.parametrize("change", ["publication", "deadline"])
def test_outgoing_discovery_discards_tentative_pointers_after_proof_loss(
    exact_relations, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    import time

    from exomem import unit_corrections

    root, _index, first, _second = exact_relations
    now = time.monotonic()
    clock = [now]
    monkeypatch.setattr(unit_corrections.time, "monotonic", lambda: clock[0])
    admitted = []

    def accept(row):
        admitted.append(row)
        if change == "publication":
            freshness.mark_external_pending(root)
        else:
            clock[0] = now + 2
        return True

    result = unit_corrections.discover(
        root, [first.ref], accept=lambda _row: True, deadline=now + 1,
        outgoing_sources={first.ref: [(first.provenance["source_hash"], first.relations)]},
        accept_outgoing=accept,
    )
    assert admitted
    assert result == {"status": "unavailable" if change == "publication" else "budget_exhausted",
                      "units": [], "outgoing": []}


def test_withheld_target_neither_preserves_a_repeated_source_nor_consumes_context(exact_relations) -> None:
    from dataclasses import replace

    from test_governance_egress import _external
    from test_relation_fragment_targets import SOURCE, TARGET, _write
    from test_unit_correction_context import _publish
    from test_working_set_currency_security import _govern

    from exomem.governance.principal import owner_principal, request_scope

    root, index, first, _second = exact_relations
    secret = "Knowledge Base/Notes/Patterns/withheld-envelope.md"
    _write(root, secret, "---\ntype: pattern\n---\n\nPrivate operating evidence.\n")
    path = root / TARGET
    path.write_text(path.read_text().replace("Rich target body.", "Operation depends on [[withheld-envelope]]."))
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    first = _selected(root, SOURCE, "first")
    profile = replace(first, level="page", ref=first.ref.partition("#")[0],
                      provenance={"source": "profile"}, relations=())
    with request_scope(owner_principal()):
        owner_packet = _packet(root, (first, profile))
    assert len(owner_packet["pointers"]) == 1
    assert first.ref in {row["ref"] for row in owner_packet["units"]}
    with request_scope(_external()):
        hidden = _packet(root, (first, profile))
        no_relation = _packet(root, (replace(first, relations=()), profile))
    assert hidden == no_relation
    assert hidden["pointers"] == []
    assert {row["ref"] for row in hidden["units"]} == {profile.ref}
    assert "withheld-envelope" not in str(hidden)


def test_withheld_outgoing_targets_do_not_exhaust_the_admitted_pointer_allowance(exact_relations) -> None:
    from test_governance_egress import _external
    from test_relation_fragment_targets import SOURCE, SOURCE_ID, TARGET, TARGET_ID, _write
    from test_unit_correction_context import _publish
    from test_working_set_currency_security import _govern

    from exomem.governance.principal import request_scope

    root, index, _first, _second = exact_relations
    _write(root, "Knowledge Base/Notes/Patterns/withheld-envelope.md",
           "---\ntype: pattern\n---\n\nPrivate operating evidence.\n")
    _write(root, TARGET, f"---\ntype: insight\nexomem_id: {TARGET_ID}\n---\n\n" + "".join(
        f"## Claim\n- category: config\n- id: hidden-{i}\n\n"
        "Operation depends on [[withheld-envelope]].\n\n" for i in range(5)
    ) + "## Claim\n- category: config\n- id: visible\n\nThe seal must pass a pressure check.\n")
    relations = ", ".join(f"depends_on: [[{TARGET}#{fragment}]]"
                          for fragment in (*[f"hidden-{i}" for i in range(5)], "visible"))
    _write(root, SOURCE, f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           f"## Claim\n- category: config\n- id: first\n- relations: {relations}\n\nThe operation may proceed.\n")
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    first = _selected(root, SOURCE, "first")
    assert len(first.relations) == 6
    with request_scope(_external()):
        packet = _packet(root, (first,))
    assert [row["ref"].partition("#")[2] for row in packet["pointers"]] == ["visible"]
    assert "withheld-envelope" not in str(packet)
    assert packet["generation"]["correction_context"] == "available"


@pytest.mark.parametrize("target_state", ["withheld", "ambiguous_withheld", "deleted"])
def test_restricted_reader_cannot_tell_a_withheld_dependency_page_from_a_deleted_one(
    exact_relations, target_state: str,
) -> None:
    from dataclasses import replace

    from test_governance_egress import _external
    from test_relation_fragment_targets import SOURCE, TARGET, _write
    from test_unit_correction_context import _publish
    from test_working_set_currency_security import _govern

    from exomem.governance.principal import request_scope

    root, index, _first, _second = exact_relations
    envelope = "Knowledge Base/Notes/Patterns/withheld-envelope.md"
    _write(root, envelope, "---\ntype: pattern\n---\n\n"
           "## Claim\n- category: config\n- id: limit\n\nPrivate operating limit.\n")
    target = f"{envelope.removesuffix('.md')}#limit"
    if target_state == "ambiguous_withheld":
        _write(root, "Knowledge Base/Notes/Patterns/legacy/withheld-envelope.md",
               "---\ntype: pattern\n---\n\n"
               "## Claim\n- category: config\n- id: limit\n\nEarlier operating limit.\n")
        target = "withheld-envelope#limit"
    path = root / SOURCE
    path.write_text(path.read_text().replace(f"{TARGET}#rich-1", target))
    if target_state == "deleted":
        (root / envelope).unlink()
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    first = _selected(root, SOURCE, "first")
    with request_scope(_external()):
        packet = _packet(root, (first,))
        no_relation = _packet(root, (replace(first, relations=()),))
    assert packet == no_relation
    assert packet["pointers"] == []
    assert packet["generation"]["correction_context"] == "available"
    assert "withheld-envelope" not in str(packet)
