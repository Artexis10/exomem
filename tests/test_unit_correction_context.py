"""Current context includes admitted corrections beyond the initial hit window."""

from pathlib import Path

from test_relation_fragment_targets import SOURCE, SOURCE_ID, TARGET, _built, _write

from exomem import (
    context_pack,
    find,
    lifecycle_statuses,
    semantic_index,
    unit_corrections,
    working_set,
)


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
