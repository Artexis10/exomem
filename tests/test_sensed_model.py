"""The sensed epistemic model: proposers, invalidation, migration, projections, replay."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import dreamer_fixture as fx
import numpy as np
import pytest
import sensing_fixture as sf

from exomem import dreamer, freshness, sensed_model, sensing, sensing_ledger, sensor_worker


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    sensed_model._VECTORS.clear()
    yield
    dreamer.reset_for_tests()
    sensor_worker.reset_for_tests()
    freshness.clear()
    sensed_model._VECTORS.clear()


def _ledger_rows(vault: Path) -> int:
    conn = sensing_ledger.open_readonly(vault)
    if conn is None:
        return 0
    try:
        return sensing_ledger.max_seq(conn)
    finally:
        conn.close()


def test_sensing_off_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    vault = sf.build(tmp_path)
    sf.settle(vault)
    assert not sensed_model.projection_path(vault).exists()
    assert not sensing_ledger.ledger_path(vault).exists()
    assert sensed_model.status_for(vault, sf.SEAL) is None


def test_a_read_shows_what_later_notes_did(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    status = sensed_model.status_for(vault, sf.SEAL)
    assert status is not None
    assert status["line"] == "refined by 1 later note; 1 open contradiction"
    assert status["refined_by_later"] == 1 and status["open_contradictions"] == 1
    assert status["evidence_complete"] is True
    [refined] = status["refined_by"]
    assert refined["page"] == sf.WINTER and refined["verdict"] == "refines"
    assert refined["instrument"]["model"] == "stub/pair-relation"
    assert refined["fixture"]["set"] == "relation-v1-multilingual"
    assert refined["reading"] and refined["p"] == 0.97
    [contra] = status["contradicted_by"]
    assert contra["page"] == sf.DENIAL and contra["verdict"] == "contradicts"
    assert status["contradiction_component"] == 2
    assert status["chain"] == [sf.SEAL, sf.WINTER]
    # The refining page does not claim to be refined by the older one.
    assert sensed_model.status_for(vault, sf.WINTER) is None
    # A neutral partner is never counted.
    assert sensed_model.status_for(vault, sf.INLET) is None


def test_an_unrelated_edit_re_senses_nothing(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    before = sf.edges(vault)
    calls = len(stub.calls)
    text = (vault / sf.SEAL).read_text(encoding="utf-8").replace(
        "## Observations", "Some new prose about the rig.\n\n## Observations"
    )
    fx.edit(vault, sf.SEAL, text)
    sf.converge(vault, stub)
    assert len(stub.calls) == calls
    assert [row[:11] for row in sf.edges(vault)] == [row[:11] for row in before]


def test_an_edited_anchored_unit_goes_stale_and_re_senses_first(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    rows = _ledger_rows(vault)
    changed = "Seal wear doubles after a dry start, above all on cold mornings."
    fx.edit(
        vault,
        sf.WINTER,
        sf.note("Seal wear in winter", "2026-05-01", changed,
                links="Builds on [[Notes/Insights/seal-wear]].", anchor="winter"),
    )
    sf.settle(vault)
    stale = [row for row in sf.edges(vault) if sf.WINTER in (row[1], row[3])]
    assert stale and {row[5] for row in stale} == {"stale"}
    conn = sensed_model.open_readonly(vault)
    queued = sensed_model.queued_pairs(conn, 10)
    priorities = dict(conn.execute("SELECT pair_key, priority FROM pairs WHERE queued=1").fetchall())
    conn.close()
    assert {pair.pair_key for pair in queued} >= {row[0] for row in stale}
    assert all(priorities[row[0]] == 0 for row in stale), "open work re-senses first"
    assert sensed_model.status_for(vault, sf.SEAL)["open_contradictions"] == 1
    assert sensed_model.status_for(vault, sf.SEAL)["evidence_complete"] is False
    assert _ledger_rows(vault) == rows, "the old reading is kept, nothing sensed yet"
    stub.table[(changed, sf.SEAL_TEXT)] = "refines"
    sf.sense(vault, stub)
    sf.settle(vault)
    status = sensed_model.status_for(vault, sf.SEAL)
    assert status["refined_by_later"] == 1 and status["evidence_complete"] is True
    assert _ledger_rows(vault) > rows


def test_a_label_map_change_re_derives_without_sensing(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    rows, calls = _ledger_rows(vault), len(stub.calls)
    strict = sensing.RelationLabelMap(
        version="relation-v1",
        columns=("entailment", "neutral", "contradiction"),
        contradicts_min=0.99,
        restates_min=0.95,
        refines_min=0.95,
    )
    monkeypatch.setitem(sensing.LABEL_MAPS, "relation-v2", strict)
    question = sensing.QUESTIONS[sensing.PAIR_RELATION]
    monkeypatch.setitem(
        sensing.QUESTIONS,
        sensing.PAIR_RELATION,
        sensing.Question(
            question.question_type, question.template_version, "relation-v2",
            question.unit_scope, question.fixture_set,
        ),
    )
    sf.settle(vault)
    status = sensed_model.status_for(vault, sf.SEAL)
    assert status["open_contradictions"] == 0
    # 0.97 on both sides is now a neutral pair, never an asymmetry.
    denial = [row for row in sf.edges(vault) if sf.DENIAL in (row[1], row[3])]
    assert {row[6] for row in denial} == {"neutral"}
    assert (_ledger_rows(vault), len(stub.calls)) == (rows, calls)


def test_a_new_pin_migrates_without_discarding(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    old = sf.StubInstrument(sf.default_table())
    sf.converge(vault, old)
    rows = _ledger_rows(vault)
    new_identity = sf.with_identity(revision="2")
    sf.enable(monkeypatch, new_identity)
    sf.settle(vault)
    states = {row[5] for row in sf.edges(vault)}
    assert states == {"migrating"}
    assert sensed_model.status_for(vault, sf.SEAL) is None
    conn = sensed_model.open_readonly(vault)
    assert {p for (p,) in conn.execute("SELECT DISTINCT priority FROM pairs WHERE queued=1")} == {0}
    conn.close()
    new = sf.StubInstrument(sf.default_table(), identity=new_identity)
    sf.sense(vault, new)
    sf.settle(vault)
    assert sensed_model.status_for(vault, sf.SEAL)["line"] == (
        "refined by 1 later note; 1 open contradiction"
    )
    assert _ledger_rows(vault) == 2 * rows, "old readings stay in the ledger"


def test_disagreeing_instruments_stay_disagreeing(tmp_path: Path, monkeypatch) -> None:
    other = sf.with_identity(model="stub/second")
    sf.enable(monkeypatch, sf.STUB, other)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    table = sf.default_table()
    table.pop((sf.DENIAL_TEXT, sf.SEAL_TEXT))
    sf.sense(vault, sf.StubInstrument(table, identity=other))
    sf.settle(vault)
    denial = [row for row in sf.edges(vault) if sf.DENIAL in (row[1], row[3])]
    assert {row[5] for row in denial} == {"instruments_disagree"}
    status = sensed_model.status_for(vault, sf.SEAL)
    assert status["open_contradictions"] == 0
    [disagreement] = status["disagreements"]
    assert sorted(v["verdict"] for v in disagreement["verdicts"]) == ["contradicts", "neutral"]


def test_a_one_way_contradiction_is_its_own_state(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    table = {(sf.DENIAL_TEXT, sf.SEAL_TEXT): "asymmetric"}
    sf.converge(vault, sf.StubInstrument(table))
    denial = [row for row in sf.edges(vault) if sf.DENIAL in (row[1], row[3])]
    assert {(row[5], row[6]) for row in denial} == {("current", "abstain")}
    assert sensed_model.status_for(vault, sf.SEAL) is None


def test_replay_into_a_fresh_projection_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    before_edges = sf.edges(vault)
    before_status = json.dumps(
        {path: sensed_model.status_for(vault, path) for path in (sf.SEAL, sf.WINTER, sf.DENIAL)},
        sort_keys=True,
    )
    calls = len(stub.calls)
    sensed_model.ProjectionStore(vault).wipe()
    sf.settle(vault)
    assert sensed_model.queue_depth(vault) == 0, "a replay senses nothing"
    assert sf.edges(vault) == before_edges
    after_status = json.dumps(
        {path: sensed_model.status_for(vault, path) for path in (sf.SEAL, sf.WINTER, sf.DENIAL)},
        sort_keys=True,
    )
    assert after_status == before_status
    assert len(stub.calls) == calls


def test_the_projection_never_reads_append_order_or_time(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    before = sf.edges(vault)
    # Rebuild the ledger with the same rows in reverse order and other times.
    conn = sensing_ledger.open_readonly(vault)
    rows = list(sensing_ledger.iter_all(conn))
    conn.close()
    path = sensing_ledger.ledger_path(vault)
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    ledger = sensing_ledger.Ledger(vault)
    lconn = ledger.connect()
    for row in reversed(rows):
        lconn.execute(
            "INSERT INTO readings SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
            "FROM readings",
            (row.reading_id, row.question_type, row.instrument_id, row.input_key,
             json.dumps(list(row.inputs)),
             json.dumps({"columns": ["entailment", "neutral", "contradiction"],
                         "directions": row.vectors,
                         "verdict": {"label": row.verdict.label, "p": row.verdict.p,
                                     "direction": row.verdict.direction,
                                     "reason": row.verdict.reason}}),
             row.verdict.label, row.label_map_version, row.fixture_set, "1999-01-01T00:00:00Z",
             row.placement),
        )
    lconn.close()
    sensed_model.ProjectionStore(vault).wipe()
    sf.settle(vault)
    assert sf.edges(vault) == before


def test_a_third_page_never_adds_or_removes_a_pair(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    pair = {row[0] for row in sf.edges(vault) if {row[1], row[3]} == {sf.SEAL, sf.WINTER}}
    assert pair
    extra = f"{sf.KB}/Notes/Insights/unrelated.md"
    fx.write(vault, extra, sf.note("Unrelated", "2026-06-01", "Grass grows in spring.",
                                   links="[[Notes/Insights/seal-wear]] [[Notes/Insights/seal-wear-winter]]"))
    freshness.on_files_changed(vault, changed=[vault / extra])
    fx.publish_graph(vault)
    sf.settle(vault)
    assert pair <= {row[0] for row in sf.edges(vault)}


def test_temporal_pairs_need_two_shared_subjects(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    links = "[[Notes/Entities/pump]] and [[Notes/Entities/rig]]"
    fx.write(vault, f"{sf.KB}/Notes/Insights/one.md", sf.note("One", "2026-01-01", "The pump hums.", links=links))
    fx.write(vault, f"{sf.KB}/Notes/Insights/two.md", sf.note("Two", "2026-02-01", "The pump is silent.", links=links))
    fx.write(vault, f"{sf.KB}/Notes/Insights/three.md",
             sf.note("Three", "2026-03-01", "The pump vibrates.", links="[[Notes/Entities/pump]]"))
    fx.seed(vault)
    fx.publish_graph(vault)
    sf.settle(vault)
    found = {frozenset((row[1], row[3])) for row in sf.edges(vault)}
    assert frozenset((f"{sf.KB}/Notes/Insights/one.md", f"{sf.KB}/Notes/Insights/two.md")) in found
    assert not any(f"{sf.KB}/Notes/Insights/three.md" in pair for pair in found)


def test_the_cosine_proposer_is_a_fixed_per_pair_threshold(tmp_path: Path, monkeypatch) -> None:
    vault = tmp_path / "vault"
    paths = [f"{sf.KB}/Notes/Insights/c{i}.md" for i in range(4)]
    texts = ["Alpha rises.", "Alpha climbs.", "Alpha ascends.", "Beta falls."]
    for i, (path, text) in enumerate(zip(paths, texts, strict=True)):
        fx.write(vault, path, sf.note(f"C{i}", f"2026-0{i + 1}-01", text))
    fx.seed(vault)
    fx.publish_graph(vault)
    base = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    near = np.array([0.9, 0.3, 0.0], dtype=np.float32)
    far = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    vecs = [base, near / np.linalg.norm(near), base, far]

    def table():
        out = {}
        conn = fx.epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
        for path, vec in zip(paths, vecs, strict=True):
            (ref, text) = conn.execute(
                "SELECT unit_ref, text FROM graph_nodes WHERE path=? AND unit_ref IS NOT NULL", (path,)
            ).fetchone()
            out[path] = {ref: (sensing.text_sha256(sensing.extract_text(text)), vec)}
        conn.close()
        return out

    sf.enable(monkeypatch, vectors=table())
    sf.settle(vault)
    found = {frozenset((row[1], row[3])) for row in sf.edges(vault)}
    assert found == {
        frozenset((paths[0], paths[1])),
        frozenset((paths[0], paths[2])),
        frozenset((paths[1], paths[2])),
    }


def test_the_dreamer_thread_never_loads_a_model_with_sensing_on(
    tmp_path: Path, monkeypatch
) -> None:
    from exomem import claims, sensing_nli

    sf.enable(monkeypatch)
    loads: list[str] = []
    monkeypatch.setattr(sensing_nli, "admit", lambda: loads.append("admit"))
    monkeypatch.setattr(claims, "_load_verifier_predictor", lambda pin: loads.append("claims"))
    vault = sf.build(tmp_path)
    sf.settle(vault)
    dreamer._loop_once(vault, dreamer.Clock())
    assert loads == []


def test_the_ledger_survives_a_projection_wipe(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    rows = _ledger_rows(vault)
    sensed_model.ProjectionStore(vault).wipe()
    assert _ledger_rows(vault) == rows
    with pytest.raises(sqlite3.IntegrityError):
        sensing_ledger.Ledger(vault).connect().execute("DELETE FROM readings")
