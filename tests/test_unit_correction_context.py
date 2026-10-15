"""Current context includes admitted corrections beyond the initial hit window."""

from pathlib import Path

import test_compiled_history as history_fixtures
import test_working_set_material as material_fixtures
from test_relation_fragment_targets import SOURCE, SOURCE_ID, TARGET, _built, _write

from exomem import (
    commands,
    context_pack,
    find,
    lexstore,
    lifecycle_statuses,
    semantic_index,
    unit_corrections,
    working_set,
    working_set_index,
    working_set_runtime,
)

material_vault = material_fixtures.material_vault
owner = history_fixtures.owner


def test_public_compiler_serves_current_connected_claims_after_guarded_corrections(
    material_vault: Path, owner,
) -> None:
    from exomem import epistemic_graph, graph_sync, vault

    root = material_vault
    parent = "Knowledge Base/Notes/Insights/calibration-account.md"
    counterpart = "Knowledge Base/Notes/Insights/validated-interval.md"
    _write(
        root, parent,
        "---\ntype: insight\nstatus: active\nproject: harbor-study\n"
        "exomem_id: 44444444-4444-4444-8444-444444444444\n---\n\n"
        "## Claim\n- category: observation\n- id: interval\n\n"
        "Violet worksheet calibration uses the coarse interval.\n\n"
        "## Claim\n- category: observation\n- id: marker\n\n"
        "Violet worksheet calibration uses the amber marker.\n\n"
        "# Appendix\n\n"
        + ("Unrelated historical background.\n" * 180),
    )
    _write(
        root, counterpart,
        "---\ntype: insight\nstatus: active\nproject: harbor-study\n"
        "exomem_id: 55555555-5555-4555-8555-555555555555\n---\n\n"
        "## Claim\n- category: fact\n- id: measured\n\n"
        "The validated value is 45 seconds.\n",
    )
    interval_ref = semantic_index.current_parent_index_state(
        root, parent
    ).document.resolve_fragment("interval").unit.unit_ref
    marker_ref = semantic_index.current_parent_index_state(
        root, parent
    ).document.resolve_fragment("marker").unit.unit_ref
    measured_ref = semantic_index.current_parent_index_state(
        root, counterpart
    ).document.resolve_fragment("measured").unit.unit_ref
    for path, reference, content, extra in (
        (parent, interval_ref, "Violet worksheet calibration uses the coarse interval.",
         {"verdict": "refuted", "check_by": "2027-01-15"}),
        (parent, marker_ref, "Violet worksheet calibration uses the measured interval.",
         {"relations": [{"kind": "contradicts", "target": f"{parent}#interval"}]}),
        (parent, marker_ref, "Violet worksheet calibration uses the calibrated marker.",
         {"relations": [{"kind": "contradicts", "target": f"{parent}#interval"}]}),
        (counterpart, measured_ref, "The validated value is 45 seconds.",
         {"relations": [{"kind": "contradicts", "target": f"{parent}#interval"}]}),
    ):
        current = commands.op_read_memory(root, path=path, unit_ref=reference)
        target = commands.op_read_memory(root, path=parent, unit_ref=interval_ref)
        commands.op_observe_memory(
            root, path=path, operation="update", category=current["unit"]["category"],
            kind="claim", unit_ref=reference,
            expected_fingerprint=current["unit"]["fingerprint"],
            expected_hash=current["parent"]["content_hash"], content=content,
            reason="The admitted measurement corrected this account.",
            target_fingerprints={interval_ref: target["unit"]["fingerprint"]}
            if extra.get("relations") else None,
            **extra,
        )
    lexstore.ensure_fresh(root)
    assert graph_sync.drain_active_rebuilds()
    epistemic_graph.EpistemicGraphIndex(root).rebuild_all()
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(root).rebuild()
    packet = commands.op_activate_context(
        root, turn="In harbor-study, what guides violet worksheet calibration?", max_chars=3000
    )
    assert packet["abstained"] is False
    assert packet["budget"]["used_chars"] <= 3000
    served = {unit["ref"]: unit for unit in packet["units"]}
    assert {interval_ref, marker_ref} <= served.keys()
    interval = served[interval_ref]
    assert interval["provenance"]["verdict"] == "refuted"
    assert interval["provenance"]["check_by"] == "2027-01-15"
    assert interval["lifecycle"] == "active"
    assert interval["provenance"]["path"] == parent
    corrections = interval["provenance"]["corrective_context"]["units"]
    assert measured_ref in {unit["unit_ref"] for unit in corrections}
    assert measured_ref not in served
    measured, = [unit for unit in corrections if unit["unit_ref"] == measured_ref]
    assert measured["family"] == "contradiction"
    assert measured["text"].strip() == "The validated value is 45 seconds."
    assert measured["source_hash"] == vault.content_hash((root / counterpart).read_text())
    assert "calibrated marker" in served[marker_ref]["text"]
    assert "Unrelated historical background" not in str(packet)
    expanded = commands.op_read_memory(root, path=parent, unit_ref=marker_ref, include_history=True)
    assert expanded["unit"]["content"].strip() == "Violet worksheet calibration uses the calibrated marker."
    assert expanded["parent"]["path"] == parent
    assert len(expanded["parent_context"]["markdown"]) <= 2400
    assert expanded["parent_context"]["truncated_after"] is True
    assert expanded["revision_history"]["returned"] >= 2, expanded["revision_history"]


def test_an_incoming_correction_is_served_when_only_old_claim_was_selected(tmp_path: Path) -> None:
    root, index = _built(tmp_path)
    _write(
        root,
        SOURCE,
        f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
        f"## Claim\n- category: config\n- id: corrected\n- relations: contradicts: [[{TARGET}#rich-1]]\n\n"
        "The controller uses the calibrated interval.\n",
    )
    index.rebuild_all()
    target = (
        semantic_index.current_parent_index_state(root, TARGET)
        .document.resolve_fragment("rich-1")
        .unit
    )
    context = unit_corrections.counterparts(root, target.unit_ref)
    assert [unit["text"].strip() for unit in context["units"]] == [
        "The controller uses the calibrated interval."
    ]
    page = find._CACHE.get(root / TARGET, root)
    hit = find._semantic_unit_hit(page, target, bm25_rank=1, bm25_score=1.0)
    item = working_set._unit_items(None, [hit])[0]
    packet = working_set.build_packet(
        items=(item,),
        anchors=(),
        roles=(),
        current_state=(),
        ambiguity=(),
        missing=(),
        max_chars=4000,
        generation={},
        status="resolved",
        status_basis=lifecycle_statuses.Basis(root),
    )
    assert packet["units"][0]["lifecycle"] == "active"
    assert packet["units"][0]["ref"] == target.unit_ref
    assert (
        packet["units"][0]["provenance"]["corrective_context"]["units"][0]["family"]
        == "contradiction"
    )
    pack = context_pack.assemble_pack(root, [hit], max_units=1, max_units_per_page=1)
    assert sum(len(group["units"]) for group in pack["semantic_units"].values()) == 1
    assert "body" not in pack["semantic_units"][TARGET]["parent"]
    assert pack["semantic_units"][TARGET]["units"][0]["corrective_context"]["units"][0][
        "unit_ref"
    ].endswith("#corrected")


def test_a_hidden_incoming_correction_answers_like_an_absent_one(tmp_path: Path) -> None:
    root, index = _built(tmp_path)
    target = (
        semantic_index.current_parent_index_state(root, TARGET)
        .document.resolve_fragment("rich-1")
        .unit
    )
    hidden = unit_corrections.counterparts(root, target.unit_ref, keep=lambda path: path != SOURCE)
    (root / SOURCE).unlink()
    index.rebuild_all()
    absent = unit_corrections.counterparts(root, target.unit_ref, keep=lambda path: path != SOURCE)
    assert hidden == absent


def test_verdict_and_revisit_date_do_not_demote_negative_knowledge(tmp_path: Path) -> None:
    root, index = _built(tmp_path)
    source = (
        (root / TARGET)
        .read_text()
        .replace("- id: rich-1", "- id: rich-1\n- verdict: refuted\n- check_by: 2027-01-15")
    )
    _write(root, TARGET, source)
    index.rebuild_all()
    unit = (
        semantic_index.current_parent_index_state(root, TARGET)
        .document.resolve_fragment("rich-1")
        .unit
    )
    page = find._CACHE.get(root / TARGET, root)
    hit = find._semantic_unit_hit(page, unit, bm25_rank=1, bm25_score=1.0)
    item = working_set._unit_items(None, [hit])[0]
    assert item.lifecycle == "active" and item.provenance["verdict"] == "refuted"
    packed, _ = context_pack._pack_unit(unit, unit_chars=800)
    assert packed["verdict"] == "refuted" and packed["check_by"] == "2027-01-15"
