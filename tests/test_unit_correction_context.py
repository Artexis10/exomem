"""Current context includes admitted corrections beyond the initial hit window."""

from pathlib import Path

import pytest
import test_compiled_history as history_fixtures
import test_working_set_material as material_fixtures
from test_relation_fragment_targets import SOURCE, SOURCE_ID, TARGET, _built, _write

from exomem import (
    commands,
    context_pack,
    find,
    freshness,
    lexstore,
    lifecycle_statuses,
    semantic_index,
    working_set,
    working_set_index,
    working_set_runtime,
)

material_vault = material_fixtures.material_vault
owner = history_fixtures.owner


def test_public_compiler_serves_current_connected_claims_after_guarded_corrections(
    material_vault: Path, owner,
) -> None:
    from exomem import epistemic_graph, graph_sync

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
    assert measured_ref in served
    measured = served[measured_ref]
    assert measured["provenance"]["family"] == "contradiction"
    assert measured["provenance"]["corrects"] == interval_ref
    assert measured["text"].strip() == "The validated value is 45 seconds."
    assert measured["provenance"]["path"] == counterpart
    exact = commands.op_read_memory(root, path=counterpart, unit_ref=measured_ref)
    assert measured["provenance"]["source_hash"] == exact["unit"]["source_hash"]
    assert packet["generation"]["correction_context"] == "available"
    from exomem._hooks import exomem_retrieve_nudge as hook

    rendered = "\n".join(hook._packet_lines(packet))
    assert "verdict=refuted" in rendered and "check_by=2027-01-15" in rendered
    assert "The validated value is 45 seconds." in rendered
    assert interval_ref in rendered and measured_ref in rendered
    assert "corrective_context" not in str(packet)
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
    _publish(index)
    target = (
        semantic_index.current_parent_index_state(root, TARGET)
        .document.resolve_fragment("rich-1")
        .unit
    )
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
    correction, = [unit for unit in packet["units"] if unit["ref"].endswith("#corrected")]
    assert correction["provenance"]["family"] == "contradiction"
    assert correction["provenance"]["corrects"] == target.unit_ref
    assert correction["text"].strip() == "The controller uses the calibrated interval."
    pack = context_pack.assemble_pack(root, [hit], max_units=2, max_units_per_page=1)
    assert sum(len(group["units"]) for group in pack["semantic_units"].values()) == 2
    assert "body" not in pack["semantic_units"][TARGET]["parent"]
    assert pack["semantic_units"][SOURCE]["units"][0]["corrects"] == target.unit_ref
    assert SOURCE not in pack["claims"]
    limited = context_pack.assemble_pack(root, [hit], max_units=1, max_units_per_page=1)
    assert sum(len(group["units"]) for group in limited["semantic_units"].values()) == 1
    assert any("pack-wide cap" in reason for reason in limited["truncation"])


def test_a_stale_selected_source_cannot_publish_its_outgoing_pointer(tmp_path: Path) -> None:
    """Final source rejection must also remove a pointer admitted through that source."""
    from exomem import ranking_config, structured_filters

    root, index = _built(tmp_path)
    _publish(index)
    lexstore.ensure_fresh(root)
    selected = find._find_semantic_units(
        root, query="", limit=1, scope="kb", mode="keyword",
        plan=structured_filters.compile_filter({"unit.category": {"$eq": "config"}}),
        snapshot=find.FreshnessSnapshot(root), prefer_active=False,
        config=ranking_config.DEFAULT_RANKING, degraded_out=[], failed_out=[],
        allowed_parent_paths={SOURCE}, _defer_hydration=True,
    )
    assert len(selected) == 1
    items = working_set._unit_items(None, selected)

    def packet():
        return working_set.build_packet(
            items=items, anchors=(), roles=(), current_state=(), ambiguity=(), missing=(),
            max_chars=4000, generation={}, status="resolved", status_basis=lifecycle_statuses.Basis(root),
        )

    before = packet()
    assert before["units"]
    assert any(pointer["ref"].endswith("#rich-1") for pointer in before["pointers"])
    source = (root / SOURCE).read_text(encoding="utf-8")
    (root / SOURCE).write_text(source.replace("Source body.", "Changed source body."), encoding="utf-8")

    after = packet()

    assert after["units"] == []
    assert after["pointers"] == []
    assert any(entry["reason"] == "lane_failed" for entry in after["missing"])


def test_hidden_corrective_units_spend_no_slots_or_lifecycle_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_governance_egress import _external
    from test_working_set_currency_security import _govern

    from exomem.governance.principal import request_scope

    root, index = _built(tmp_path)
    _write(root, "Knowledge Base/Notes/Patterns/withheld-proof.md", "---\ntype: pattern\n---\n\nWithheld evidence.\n")
    header = f"---\ntype: insight\nstatus: active\nexomem_id: {SOURCE_ID}\n---\n\n"
    hidden = "".join(
        f"## Claim\n- category: config\n- id: hidden-{i}\n"
        f"- relations: supersedes: [[{TARGET}#rich-1]]\n\n"
        "The interval depends on [[withheld-proof]].\n\n"
        for i in range(5)
    )
    valid = (f"## Claim\n- category: config\n- id: visible\n"
             f"- relations: contradicts: [[{TARGET}#rich-1]]\n\n"
             "The admitted value is 45 seconds.\n")
    _write(root, SOURCE, header + hidden + valid)
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    with request_scope(_external()):
        hidden_packet = _target_packet(root)
        target = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit
        hit = find._semantic_unit_hit(find._CACHE.get(root / TARGET, root), target, bm25_rank=1, bm25_score=1.0)
        pack = context_pack.assemble_pack(root, [hit], max_units=2, max_units_per_page=1)
    assert "withheld-proof" not in str(hidden_packet)
    assert {unit["ref"].split("#")[-1] for unit in hidden_packet["units"]} == {"rich-1", "visible"}
    assert all(unit["lifecycle"] == "active" for unit in hidden_packet["units"])
    assert "withheld-proof" not in str(pack)
    assert {unit["unit_ref"].split("#")[-1] for group in pack["semantic_units"].values()
            for unit in group["units"]} == {"rich-1", "visible"}
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS", "2")
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS_PER_PAGE", "1")
    wire = _public_deep(root)
    assert "withheld-proof" not in str(wire)
    assert {unit["unit_ref"].split("#")[-1] for group in wire["pack"]["semantic_units"].values()
            for unit in group["units"]} == {"rich-1", "visible"}
    _write(root, SOURCE, header + valid)
    _publish(index)
    with request_scope(_external()):
        absent_packet = _target_packet(root)
    assert hidden_packet == absent_packet


def _publish(index) -> None:
    # Fixture publication warms the existing resolver; optional reads never build it.
    root = index.vault_root
    from exomem import vault

    freshness.seed(root, "vault", ((str(path), freshness.stat_signature(path))
                                  for path in vault.walk_vault_md(root)))
    find.evict_resolver_caches(root)
    index.rebuild_all()
    find.recall_resolver_snapshot(root, expected_checkpoint=freshness.live_recall_checkpoint(root, "vault"))


def _target_packet(root: Path, max_chars: int = 4000) -> dict:
    target = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit
    page = find._CACHE.get(root / TARGET, root)
    hit = find._semantic_unit_hit(page, target, bm25_rank=1, bm25_score=1.0)
    return working_set.build_packet(
        items=working_set._unit_items(None, [hit]), anchors=(), roles=(), current_state=(),
        ambiguity=(), missing=(), max_chars=max_chars, generation={}, status="resolved",
        status_basis=lifecycle_statuses.Basis(root),
    )


def test_historical_counterpart_is_labelled_without_replacing_current_claim(tmp_path: Path) -> None:
    from exomem._hooks import exomem_retrieve_nudge as hook

    root, index = _built(tmp_path)
    _write(root, SOURCE,
           f"---\ntype: insight\nstatus: superseded\nexomem_id: {SOURCE_ID}\n---\n\n"
           f"## Claim\n- category: config\n- id: historical\n"
           f"- relations: supersedes: [[{TARGET}#rich-1]]\n\nThe previous value was 45 seconds.\n")
    _publish(index)
    packet = _target_packet(root)
    served = {unit["ref"].split("#")[-1]: unit for unit in packet["units"]}
    assert served["rich-1"]["lifecycle"] == "active"
    assert served["historical"]["history"] is True
    assert served["historical"]["lifecycle"] == "superseded"
    rendered = "\n".join(hook._packet_lines(packet))
    assert "history" in rendered and "The previous value was 45 seconds." in rendered


def test_corrective_prose_cannot_bypass_the_packet_budget(tmp_path: Path) -> None:
    from exomem._hooks import exomem_retrieve_nudge as hook

    root, index = _built(tmp_path)
    _write(root, TARGET, (root / TARGET).read_text().replace(
        "- id: rich-1", "- id: rich-1\n- verdict: refuted\n- check_by: 2027-01-15"))
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           f"## Claim\n- category: config\n- id: corrected\n"
           f"- relations: contradicts: [[{TARGET}#rich-1]]\n\n"
           + "The measurement remains supported. " * 20 + "\n")
    _publish(index)
    packet = _target_packet(root, max_chars=500)
    assert sum(len(unit["text"]) for unit in packet["units"]) <= packet["budget"]["used_chars"] <= 500
    assert not any(unit["ref"].endswith("#corrected") for unit in packet["units"])
    assert any(pointer["ref"].endswith("#corrected") for pointer in packet["pointers"])
    rendered = "\n".join(hook._packet_lines(packet))
    assert "verdict=refuted" in rendered and "check_by=2027-01-15" in rendered
    assert "The measurement remains supported." not in rendered


def test_verdict_and_revisit_date_do_not_demote_negative_knowledge(tmp_path: Path) -> None:
    root, index = _built(tmp_path)
    source = (
        (root / TARGET)
        .read_text()
        .replace("- id: rich-1", "- id: rich-1\n- verdict: refuted\n- check_by: 2027-01-15")
    )
    _write(root, TARGET, source)
    _publish(index)
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


@pytest.mark.parametrize("state", ["live", "cold", "inherited", "resolver_absent", "publication", "sqlite_busy", "barrier", "malformed_ack", "external_pending"])
def test_optional_discovery_uses_only_a_current_nonblocking_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    import json
    import sqlite3

    from exomem import epistemic_graph, unit_corrections

    root, index = _built(tmp_path)
    _publish(index)
    reference = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit.unit_ref

    def cold_work(*_args, **_kwargs):
        pytest.fail("optional compiler discovery started cold proof, adoption or a vault walk")

    for owner, name in (
        (freshness, "recall_checkpoint"), (freshness, "adopt_recall_origin"),
        (epistemic_graph, "_disk_vault_freshness"),
        (epistemic_graph.EpistemicGraphIndex, "_snapshot_sources_match_disk"),
        (epistemic_graph, "_await_publication_hold"), (find, "recall_resolver_snapshot"),
    ):
        monkeypatch.setattr(owner, name, cold_work)
    hold = None
    lock = None
    try:
        if state == "cold":
            freshness.invalidate(root)
        elif state == "resolver_absent":
            monkeypatch.setattr(find, "recall_resolver_snapshot_at_checkpoint", lambda *_args: None)
        elif state == "publication":
            hold = epistemic_graph._acquire_publication_hold(index.path)
            assert hold is not None
        elif state == "external_pending":
            freshness.mark_external_pending(root)
        elif state in {"inherited", "barrier", "malformed_ack"}:
            with sqlite3.connect(index.path) as conn:
                if state == "inherited":
                    value = json.loads(conn.execute("SELECT value FROM graph_meta WHERE key='recall_projection_checkpoint'").fetchone()[0])
                    value[0] = "another-process"
                    key, value = "recall_projection_checkpoint", json.dumps(value)
                else:
                    key, value = ("read_barrier", "pending") if state == "barrier" else ("graph_sync_generation", "invalid")
                conn.execute("INSERT OR REPLACE INTO graph_meta(key,value) VALUES (?,?)", (key, value))
        elif state == "sqlite_busy":
            lock = sqlite3.connect(index.path)
            lock.execute("PRAGMA journal_mode=DELETE")
            lock.execute("BEGIN EXCLUSIVE")
        result = unit_corrections.discover(root, [reference], accept=lambda _row: True)
        assert result["status"] == ("available" if state == "live" else "unavailable")
        assert bool(result["units"]) is (state == "live")
    finally:
        if lock is not None:
            lock.close()
        epistemic_graph._release_publication_hold(hold)


@pytest.mark.parametrize("change", ["publication", "deadline"])
def test_discovery_discards_all_tentative_units_when_hydration_loses_its_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    import time

    from exomem import unit_corrections

    root, index = _built(tmp_path)
    _publish(index)
    reference = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit.unit_ref
    now = time.monotonic()
    clock = [now]
    monkeypatch.setattr(unit_corrections.time, "monotonic", lambda: clock[0])
    admitted = []

    def accept(row):
        admitted.append(row["unit_ref"])
        if change == "publication":
            freshness.mark_external_pending(root)
        else:
            clock[0] = now + 2
        return True

    result = unit_corrections.discover(root, [reference], accept=accept, deadline=now + 1)
    assert admitted
    assert result == {"status": "unavailable" if change == "publication" else "budget_exhausted", "units": []}


def test_sql_discovery_stops_at_the_shared_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    from exomem import epistemic_graph, unit_corrections

    root, index = _built(tmp_path)
    _publish(index)
    reference = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit.unit_ref
    now = time.monotonic()
    clock = [now]
    finished = []
    monkeypatch.setattr(unit_corrections.time, "monotonic", lambda: clock[0])

    def expensive_scan(view, *_args, **_kwargs):
        clock[0] = now + 2
        view.conn.execute("WITH RECURSIVE n(x) AS (SELECT 0 UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n").fetchone()
        finished.append(True)
        return [], False

    monkeypatch.setattr(epistemic_graph.GraphView, "neighbor_edges", expensive_scan)
    result = unit_corrections.discover(root, [reference], accept=lambda _row: True, deadline=now + 1)
    assert result == {"status": "budget_exhausted", "units": []}
    assert not finished


@pytest.mark.parametrize("optional", ["relation", "context", "parent"])
def test_public_deep_pack_projects_optional_references_before_spending_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, optional: str,
) -> None:
    import json

    from test_working_set_currency_security import _govern

    root, index = _built(tmp_path)
    _write(root, "Knowledge Base/Notes/Patterns/withheld-proof.md",
           "---\ntype: pattern\naliases: [Withheld record]\n---\n\nWithheld evidence.\n")
    provenance = ("sources: ['[[withheld-proof]]']\nevidence: ['[[Withheld record]]']\n"
                  "supersedes: ['[[withheld-proof]]']\n") if optional == "parent" else ""
    extra_context = "- context: [[Withheld record#detail|record]]\n" if optional == "context" else ""
    relation = f"contradicts: [[{TARGET}#rich-1]]"
    if optional == "relation":
        relation += ", supports: [[withheld-proof.md#detail|record]]"
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n{provenance}---\n\n"
           "## Claim\n- category: withheld-proof\n- id: correction\n- tags: withheld-proof\n"
           f"{extra_context}- relations: {relation}\n\nThe validated value is 45 seconds.\n")
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS", "2")
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS_PER_PAGE", "1")
    wire = _public_deep(root)
    pack = wire["pack"]
    assert len(wire["hits"]) == 1
    assert sum(len(group["units"]) for group in pack["semantic_units"].values()) == 2
    group = pack["semantic_units"][SOURCE]
    unit, = group["units"]
    assert unit["excerpt"] == "The validated value is 45 seconds."
    assert unit["category_raw"] == "withheld-proof" and unit["tags"] == ["withheld-proof"]
    assert len(unit["relations"]) == 1 and unit["relations"][0]["kind"] == "contradicts"
    emitted = json.dumps({"unit": group, "legacy": pack["semantic_blocks"][SOURCE]})
    assert "[[withheld-proof" not in emitted and "[[Withheld record" not in emitted
    assert "Knowledge Base/Notes/Patterns/withheld-proof" not in emitted
    assert not group["parent"]["sources"] and not group["parent"]["evidence"]
    assert not group["parent"]["supersedes"]


def _public_deep(root: Path) -> dict:
    from test_governance_egress import _external

    from exomem import writer_lease
    from exomem.governance.principal import request_scope

    command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "ask_memory")
    with request_scope(_external()):
        return writer_lease.invoke_command(
            command, root, query="Rich target body", deep=True, result_level="unit",
            limit=1, mode="keyword", graph=False, rerank=False, scope="kb-only", detail="full",
        )


def test_public_deep_pack_admits_only_the_clipped_prose(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_working_set_currency_security import _govern

    root, index = _built(tmp_path)
    _write(root, "Knowledge Base/Notes/Patterns/withheld-proof.md", "---\ntype: pattern\n---\n\nWithheld evidence.\n")
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           f"## Claim\n- category: config\n- id: correction\n- relations: contradicts: [[{TARGET}#rich-1]]\n\n"
           "The validated value is 45 seconds. " + "Measurement background. " * 30 + "[[withheld-proof]]\n")
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    monkeypatch.setenv("EXOMEM_PACK_UNIT_CHARS", "80")
    wire = _public_deep(root)
    unit, = wire["pack"]["semantic_units"][SOURCE]["units"]
    assert unit["excerpt"].startswith("The validated value is 45 seconds.")
    assert len(unit["excerpt"]) <= 81 and unit["excerpt"].endswith("…")
    assert "[[withheld-proof]]" not in str(wire)


def test_public_deep_pack_propagates_unavailable_reference_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_working_set_currency_security import _govern

    from exomem.governance import egress

    root, index = _built(tmp_path)
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           "## Claim\n- category: config\n- id: correction\n- context: [[Unavailable record]]\n"
           f"- relations: contradicts: [[{TARGET}#rich-1]]\n\nThe validated value is 45 seconds.\n")
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)

    def unavailable(*_args):
        raise egress.WorkingSetResolutionUnavailable("reference index unavailable")

    monkeypatch.setattr(egress, "_resolved_prose_names", unavailable)
    with pytest.raises(egress.ReaderViewUnavailable):
        _public_deep(root)


def test_public_deep_pack_keeps_notice_level_body_admission(tmp_path: Path) -> None:
    from test_governance_egress import write_rule
    from test_working_set_currency_security import _govern
    from test_working_set_egress import _reset_governance_state

    root, index = _built(tmp_path)
    _write(root, "Knowledge Base/Notes/Patterns/withheld-proof.md", "---\ntype: pattern\n---\n\nWithheld evidence.\n")
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           "## Claim\n- category: config\n- id: correction\n"
           f"- relations: contradicts: [[{TARGET}#rich-1]]\n\n"
           "The interval depends on [[withheld-proof]].\n")
    _govern(root)
    write_rule(root, ceiling=1, audience="external")
    _reset_governance_state(root)
    lexstore.ensure_fresh(root)
    _publish(index)
    wire = _public_deep(root)
    assert "withheld-proof" not in str(wire)
    unit, = wire["pack"]["semantic_units"][SOURCE]["units"]
    assert unit["unit_ref"].endswith("#correction") and "excerpt" not in unit
    block, = wire["pack"]["semantic_blocks"][SOURCE]
    assert "body" not in block


@pytest.mark.parametrize("meaning", ["alias", "deprecated"])
def test_corrective_counterparts_share_graph_scope_before_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, meaning: str,
) -> None:
    import yaml

    root, index = _built(tmp_path)
    allowed = {"parent": "supports", "description": "Supports the measured interval"}
    if meaning == "alias":
        allowed["aliases"] = ["measured_support"]
        label = "measured_support"
    else:
        allowed.update(status="deprecated", replaced_by="supports")
        label = "study.allowed"
    _write(root, "Knowledge Base/_Schema/relation-registry.yaml", yaml.safe_dump({
        "schema_version": 1,
        "extensions": {
            "study.scoped": {"parent": "supports", "description": "Supports an experiment",
                             "scope": {"page_types": ["experiment"]}},
            "study.allowed": allowed,
        },
    }))
    outside_scope = "".join(
        f"## Claim\n- category: config\n- id: outside-{number}\n"
        f"- relations: study.scoped: [[{TARGET}#rich-1]]\n\n"
        "This experiment-only relation was authored on an insight.\n\n"
        for number in range(5)
    )
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n" + outside_scope
           + "## Claim\n- category: config\n- id: admitted\n"
           + f"- relations: {label}: [[{TARGET}#rich-1]]\n\nThe measured interval is 45 seconds.\n")
    lexstore.ensure_fresh(root)
    _publish(index)
    target = semantic_index.current_parent_index_state(root, TARGET).document.resolve_fragment("rich-1").unit
    graph = commands.op_graph_context(root, unit_ref=target.unit_ref)
    meanings = {edge["relation_type"] for edge in graph["edges"]}
    assert "study.allowed" in meanings and "study.scoped" not in meanings
    packet = _target_packet(root)
    assert {unit["ref"].split("#")[-1] for unit in packet["units"]} == {"rich-1", "admitted"}
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS", "2")
    monkeypatch.setenv("EXOMEM_PACK_MAX_UNITS_PER_PAGE", "1")
    wire = _public_deep(root)
    assert {unit["unit_ref"].split("#")[-1] for group in wire["pack"]["semantic_units"].values()
            for unit in group["units"]} == {"rich-1", "admitted"}


@pytest.mark.parametrize("target", [f"{TARGET}#rich-1", "Frag target#rich-1"])
def test_incoming_supersession_borrows_the_resident_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    from exomem import vault

    root, index = _built(tmp_path)
    _write(root, SOURCE,
           f"---\ntype: insight\nexomem_id: {SOURCE_ID}\n---\n\n"
           f"## Claim\n- category: config\n- id: successor\n- relations: supersedes: [[{target}]]\n\n"
           "The measured interval is 45 seconds.\n")
    _publish(index)
    builds = []
    original = vault.WikilinkResolver._build

    def build(resolver):
        builds.append(True)
        return original(resolver)

    monkeypatch.setattr(vault.WikilinkResolver, "_build", build)
    packet = _target_packet(root)
    assert {unit["ref"].split("#")[-1] for unit in packet["units"]} == {"rich-1", "successor"}
    assert all(unit["lifecycle"] == "active" for unit in packet["units"])
    assert not builds, "qualified incoming supersession must not walk Markdown during compilation"


def _sibling_items(root: Path, target: str) -> tuple:
    _write(root, TARGET, (root / TARGET).read_text() + (
        "\n## Claim\n- category: config\n- id: successor\n"
        f"- relations: supersedes: [[{target}]]\n\nThe measured interval is 45 seconds.\n"
    ))
    state = semantic_index.current_parent_index_state(root, TARGET)
    page = find._CACHE.get(root / TARGET, root)
    return working_set._unit_items(None, [
        find._semantic_unit_hit(page, state.document.resolve_fragment(anchor).unit,
                                bm25_rank=1, bm25_score=1.0)
        for anchor in ("rich-1", "successor")
    ])


def test_local_supersession_needs_no_resident_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from exomem import working_set_currency

    root, _index = _built(tmp_path)
    items = _sibling_items(root, "#rich-1")

    def unavailable(*_args):
        raise AssertionError("local anchor currency does not need page resolution")

    monkeypatch.setattr(find, "recall_resolver_snapshot_at_checkpoint", unavailable)
    annotated = working_set_currency.annotate(items, status_basis=lifecycle_statuses.Basis(root))
    assert [item.lifecycle for item in annotated] == ["superseded", "active"]


@pytest.mark.parametrize("state", ["missing", "moving"])
def test_currency_cannot_publish_without_current_qualified_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    from exomem import vault

    root, index = _built(tmp_path)
    items = _sibling_items(root, f"{TARGET}#rich-1")
    _publish(index)
    if state == "missing":
        monkeypatch.setattr(find, "recall_resolver_snapshot_at_checkpoint", lambda *_args: None)
    else:
        original = vault.normalize_wikilink

        def move(*args, **kwargs):
            result = original(*args, **kwargs)
            freshness.mark_external_pending(root)
            return result

        monkeypatch.setattr(vault, "normalize_wikilink", move)
    recent = [{"path": TARGET, "title": "Frag target", "type": "insight", "status": "active"}]
    packet = working_set.build_packet(
        items=items, anchors=(), roles=(), current_state=(), ambiguity=(), missing=(),
        max_chars=4000, generation={"continuity": "applied", "source": "fixture"},
        status="resolved", recent_context=recent, status_basis=lifecycle_statuses.Basis(root),
    )
    assert packet["abstained"] and packet["abstention"]["reason"] == "unavailable"
    assert not packet["units"] and not packet["pointers"] and not packet["current_state"]
    assert packet["generation"]["continuity"] == "applied" and packet["recent_context"] == recent


def test_withheld_namesake_does_not_change_same_page_currency(tmp_path: Path) -> None:
    from test_governance_egress import _external
    from test_working_set_currency_security import _govern

    from exomem.governance.principal import request_scope

    root, index = _built(tmp_path)
    items = _sibling_items(root, "frag-target#rich-1")
    namesake = _write(root, "Knowledge Base/Notes/Patterns/frag-target.md",
                      "---\ntype: pattern\n---\n\nWithheld namesake.\n")
    _govern(root)
    lexstore.ensure_fresh(root)
    _publish(index)

    def packet():
        with request_scope(_external()):
            return working_set.build_packet(
                items=items, anchors=(), roles=(), current_state=(), ambiguity=(), missing=(),
                max_chars=4000, generation={}, status="resolved", status_basis=lifecycle_statuses.Basis(root),
            )

    with_namesake = packet()
    old = next(unit for unit in with_namesake["units"] if unit["ref"].endswith("#rich-1"))
    assert old["lifecycle"] == "superseded" and old["history"]
    namesake.unlink()
    lexstore.ensure_fresh(root)
    _publish(index)
    assert packet() == with_namesake
