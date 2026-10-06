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


def test_a_third_page_never_moves_a_pairs_selection(tmp_path: Path, monkeypatch) -> None:
    """The cap is per page pair. An aside that links only the hub must not move
    which hub-later pairs are sensed: under the per-page cap its 12 pairs took
    the hub past 128 and pushed four hub-later pairs out. A page pair past the
    cap keeps exactly its first PAIR_CAP pairs in the fixed order."""
    sf.enable(monkeypatch)
    monkeypatch.setattr(sensed_model, "PAIR_CAP", 100, raising=False)
    vault = sf.hub_vault(tmp_path, with_page=False)
    sf.settle(vault)

    def hub_later() -> dict[str, int]:
        pages = {sf.HUB, sf.LATER}
        return {row[0]: row[11] for row in sf.edges(vault) if {row[1], row[3]} == pages}

    before = hub_later()
    assert len(before) == 120
    fx.edit(vault, sf.ASIDE, sf.aside())
    sf.settle(vault)
    assert hub_later() == before
    conn = sensed_model.open_readonly(vault)
    first = [key for (key,) in conn.execute(
        "SELECT pair_key FROM pairs WHERE ? IN (path_a, path_b) AND ? IN (path_a, path_b) "
        "ORDER BY order_key LIMIT 100", (sf.HUB, sf.LATER))]
    aside = conn.execute(
        "SELECT count(*), sum(selected) FROM pairs WHERE ? IN (path_a, path_b)", (sf.ASIDE,)
    ).fetchone()
    conn.close()
    assert {key for key, selected in before.items() if selected} == set(first)
    assert aside == (12, 12)


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


def test_a_partner_edited_since_the_last_tick_is_not_served_as_complete(
    tmp_path: Path, monkeypatch
) -> None:
    """MEDIUM 2, the reviewer's repro: DENIAL stops contradicting; SEAL is read
    before any tick. The stale edge is dropped and the evidence is incomplete."""
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    fx.edit(
        vault,
        sf.DENIAL,
        sf.note("Seal wear denial", "2026-05-02", "Seal wear is worth measuring.",
                links="Disputes [[Notes/Insights/seal-wear]]."),
    )
    status = sensed_model.status_for(vault, sf.SEAL)
    assert status is not None and status["open_contradictions"] == 0
    assert status["evidence_complete"] is False
    assert status["line"] == "refined by 1 later note"


def test_a_changed_instrument_is_not_served_before_the_projection_follows(
    tmp_path: Path, monkeypatch
) -> None:
    """MEDIUM 2: edges projected under another active instrument set are dropped."""
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    sf.enable(monkeypatch, sf.with_identity(revision="2"))
    # Every edge was dropped: the page says its evidence is incomplete rather
    # than going silent (recheck of slice 1, M2 residual).
    assert sensed_model.status_for(vault, sf.SEAL) == {
        "refined_by_later": 0,
        "open_contradictions": 0,
        "evidence_complete": False,
    }


def test_a_page_with_nothing_dropped_and_nothing_to_say_stays_silent(
    tmp_path: Path, monkeypatch
) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    assert sensed_model.status_for(vault, sf.INLET) is None


NEWER = f"{sf.KB}/Notes/Insights/seal-wear-revised.md"


def _superseding_note(created: str) -> str:
    return (
        "---\n"
        "title: Seal wear revised\n"
        "type: insight\n"
        "status: active\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        'supersedes: "[[Notes/Insights/seal-wear]]"\n'
        "---\n\n"
        "# Seal wear revised\n\n"
        "## Observations\n\n"
        "- [finding] Pump housings are cast in one piece.\n"
    )


def test_a_supersession_partner_that_moved_leaves_the_chain_before_a_tick(
    tmp_path: Path, monkeypatch
) -> None:
    """Recheck concern 1: chain members come from live pages only."""
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    fx.write(vault, NEWER, _superseding_note("2026-06-01"))
    fx.seed(vault)
    fx.publish_graph(vault)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    assert sensed_model.status_for(vault, sf.SEAL)["chain"] == [sf.SEAL, sf.WINTER, NEWER]
    # Re-dated before the page it supersedes: its projected date is stale.
    fx.edit(vault, NEWER, _superseding_note("2026-01-01"))
    assert sensed_model.status_for(vault, sf.SEAL)["chain"] == [sf.SEAL, sf.WINTER]
    fx.remove(vault, NEWER)
    assert sensed_model.status_for(vault, sf.SEAL)["chain"] == [sf.SEAL, sf.WINTER]


def _served_fingerprint(**changes) -> str:
    from exomem import embedding_backend as eb

    served = eb.served_artifact("BAAI/bge-m3")
    query, passage, pooling, pad = eb._DECLARED["BAAI/bge-m3"]
    fields = dict(
        model="BAAI/bge-m3", pooling=pooling, query_prefix=query, passage_prefix=passage,
        max_seq=eb.SERVED_MAX_SEQ, pad_token=pad, revision=served.revision,
        quantization=served.quantization, file_format=served.file_format,
        artifact_digest=served.digest[:16],
    )
    fields.update(changes)
    return eb.EncoderProfile(**fields).fingerprint()


def test_theta_is_keyed_by_the_exact_encoder_fingerprint() -> None:
    """LOW 8 (recheck residual): a threshold belongs to one exact vector space.

    Quantisation, sequence limit, pooling and the served bytes are all part of
    it, and anything else, the model's name or prefix included, proposes nothing.
    """
    served = _served_fingerprint()
    assert sensed_model.theta_for(served) == 0.72
    assert sf.ENCODER == served
    for other in (
        _served_fingerprint(quantization=None, file_format=None, artifact_digest=None),
        _served_fingerprint(max_seq=8192),
        _served_fingerprint(pooling="mean"),
        _served_fingerprint(artifact_digest="0" * 16),
        "BAAI/bge-m3|cls|l2",
        "BAAI/bge-m3|cls|l2|0123456789abcdef",
        "BAAI/bge-m3",
        None,
    ):
        assert sensed_model.theta_for(other) is None, other


def _cosine_vault(tmp_path: Path, count: int = 4):
    vault = tmp_path / "vault"
    paths = [f"{sf.KB}/Notes/Insights/c{i}.md" for i in range(count)]
    for i, path in enumerate(paths):
        fx.write(vault, path, sf.note(f"C{i}", f"2026-0{i + 1}-01", f"Alpha variant {i}."))
    fx.seed(vault)
    fx.publish_graph(vault)
    return vault, paths


def _vector_table(vault: Path, paths: list[str], vectors: list) -> dict:
    out = {}
    conn = fx.epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    for path, vec in zip(paths, vectors, strict=True):
        ref, text = conn.execute(
            "SELECT unit_ref, text FROM graph_nodes WHERE path=? AND unit_ref IS NOT NULL", (path,)
        ).fetchone()
        out[path] = {ref: (sensing.text_sha256(sensing.extract_text(text)), vec)}
    conn.close()
    return out


def test_the_cosine_predicate_compares_the_rounded_cosine(tmp_path: Path, monkeypatch) -> None:
    """LOW 8: the recorded cosine and the predicate agree exactly."""
    vault, paths = _cosine_vault(tmp_path, 2)
    theta = 0.72
    # A raw cosine of 0.71999976 rounds to 0.72 at six decimals.
    angle = np.arccos(np.float64(0.71999976))
    base = np.array([1.0, 0.0], dtype=np.float32)
    turned = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)
    raw = float(base @ turned)
    assert raw < theta and round(raw, 6) == theta
    sf.enable(monkeypatch, vectors=_vector_table(vault, paths, [base, turned]))
    sf.settle(vault)
    rows = sensed_model.open_readonly(vault).execute("SELECT proposer, cosine FROM pairs").fetchall()
    assert rows == [("cosine", 0.72)]


def test_over_the_cache_bound_the_same_cosine_pairs_are_proposed(
    tmp_path: Path, monkeypatch
) -> None:
    """The vector bound limits memory, never proposals. Over it, ticks read the
    stored vectors in blocks; under the old bound every cosine pair was dropped,
    so a withheld page's units could take a visible pair away."""
    vault, paths = _cosine_vault(tmp_path, 4)
    same = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    sf.enable(monkeypatch, vectors=_vector_table(vault, paths, [same, same, same, same]))
    monkeypatch.setattr(sensed_model, "MAX_COSINE_UNITS", 1)
    # Two pages a tick: later ticks meet earlier pages' stored rows.
    monkeypatch.setattr(sensed_model, "PAGES_PER_TICK", 2)
    sf.settle(vault)
    found = {frozenset((row[1], row[3])) for row in sf.edges(vault)}
    assert found == {frozenset((a, b)) for a in paths for b in paths if a < b}


def test_one_pass_over_the_vectors_serves_every_page_of_a_tick(
    tmp_path: Path, monkeypatch
) -> None:
    """Over the bound, the pages a tick processes share one read of the stored
    vectors. Catches a per-page rescan regression: a full pass per page is a
    CPU burst on the owner's desktop that sharing costs a fraction of. The pass
    predates the tick, so a page applied earlier in it is met by its new
    vector, never its old row."""
    vault, paths = _cosine_vault(tmp_path, 4)
    same = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    table = _vector_table(vault, paths, [same, same, same, same])
    sf.enable(monkeypatch, vectors=table)
    monkeypatch.setattr(sensed_model, "MAX_COSINE_UNITS", 1)
    sf.settle(vault)
    reads: list[int] = []
    read_blocks = sensed_model._read_blocks
    monkeypatch.setattr(
        sensed_model, "_read_blocks", lambda conn: (reads.append(1), read_blocks(conn))[1]
    )
    # c0 moves away from the others; c0, c1 and c2 are processed in one tick.
    (ref, (digest, _vector)), = table[paths[0]].items()
    table[paths[0]] = {ref: (digest, np.array([0.0, 0.0, 1.0], dtype=np.float32))}
    for path in paths[:3]:
        fx.edit(vault, path, (vault / path).read_text(encoding="utf-8") + "\nA later line.\n")
    sf.settle(vault)
    assert len(reads) == 1, "three pages in one tick, one pass"
    found = {frozenset((row[1], row[3])) for row in sf.edges(vault)}
    assert found == {frozenset((a, b)) for a in paths[1:] for b in paths[1:] if a < b}


def test_a_new_encoder_fingerprint_re_proposes_cosine_pairs(tmp_path: Path, monkeypatch) -> None:
    vault, paths = _cosine_vault(tmp_path, 2)
    same = np.array([1.0, 0.0], dtype=np.float32)
    sf.enable(monkeypatch, vectors=_vector_table(vault, paths, [same, same]))
    sf.settle(vault)
    assert len(sf.edges(vault)) == 1
    monkeypatch.setattr(sensed_model, "encoder_fingerprint", lambda _v: "BAAI/bge-m3|cls|l2|ffff")
    monkeypatch.setattr(sensed_model, "stored_unit_vectors", lambda _v, _r, _fp: {})
    sf.settle(vault)
    assert sf.edges(vault) == [], "vectors of another encoder propose nothing"


def test_pages_in_one_tick_find_the_cosine_pairs_separate_ticks_find(
    tmp_path: Path, monkeypatch
) -> None:
    """Pages processed in one tick meet each other in memory, not through the
    stored vectors. Both routes must find the same pairs with the same cosines,
    a pair whose float32 score falls just under θ but rounds to it included."""
    vault = tmp_path / "vault"
    paths = [f"{sf.KB}/Notes/Insights/c{i}.md" for i in range(6)]
    for i, path in enumerate(paths):
        fx.write(vault, path, sf.note(f"C{i}", f"2026-0{i + 1}-01", f"Alpha variant {i}.",
                                      extra=f"- [finding] Beta variant {i}.\n"))
    fx.seed(vault)
    fx.publish_graph(vault)
    rng = np.random.default_rng(7)
    vectors = [v / np.linalg.norm(v) for v in rng.standard_normal((12, 3)).astype(np.float32)]
    angle = np.arccos(np.float64(0.71999976))
    vectors[0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    vectors[2] = np.array([np.cos(angle), np.sin(angle), 0.0], dtype=np.float32)
    unit_vectors = iter(vectors)
    conn = fx.epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    table: dict = {}
    for path in paths:
        for ref, text in conn.execute(
            "SELECT unit_ref, text FROM graph_nodes WHERE path=? AND unit_ref IS NOT NULL "
            "ORDER BY unit_ref", (path,)
        ):
            digest = sensing.text_sha256(sensing.extract_text(text))
            table.setdefault(path, {})[ref] = (digest, next(unit_vectors))
    conn.close()
    sf.enable(monkeypatch, vectors=table)

    def cosine_pairs() -> list[tuple]:
        conn = sensed_model.open_readonly(vault)
        rows = conn.execute("SELECT pair_key, cosine FROM pairs ORDER BY pair_key").fetchall()
        conn.close()
        return rows

    monkeypatch.setattr(sensed_model, "PAGES_PER_TICK", 1)
    sf.settle(vault, rounds=10)
    apart = cosine_pairs()
    sensed_model.ProjectionStore(vault).wipe()
    monkeypatch.setattr(sensed_model, "PAGES_PER_TICK", 16)
    sf.settle(vault)
    assert cosine_pairs() == apart
    assert 0.72 in {cosine for _key, cosine in apart}


def _requeue_every_page(vault: Path) -> dict[str, str]:
    """Mark every projected page changed, and return the dreamer's `seen` map."""
    from exomem import dreamer_store

    conn = sqlite3.connect(sensed_model.projection_path(vault), isolation_level=None)
    conn.execute("UPDATE pages SET sig=NULL")
    conn.close()
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        return store.seen_map(conn)
    finally:
        conn.close()


def test_a_budget_spent_by_the_pass_still_applies_the_pages_it_was_read_for(
    tmp_path: Path, monkeypatch
) -> None:
    """Catches a livelock: if a tick's budget stop discarded the pages after
    their shared pass, every tick over the bound would read every vector, apply
    nothing and repeat."""
    vault, paths = _cosine_vault(tmp_path, 4)
    same = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    sf.enable(monkeypatch, vectors=_vector_table(vault, paths, [same, same, same, same]))
    sf.settle(vault)
    monkeypatch.setattr(sensed_model, "MAX_COSINE_UNITS", 1)
    seen = _requeue_every_page(vault)
    reads: list[int] = []
    read_blocks = sensed_model._read_blocks
    monkeypatch.setattr(
        sensed_model, "_read_blocks", lambda conn: (reads.append(1), read_blocks(conn))[1]
    )
    report = sensed_model.run_tick(vault, seen=seen, halt=lambda: "cpu" if reads else None)
    assert len(reads) == 1 and report.stop is None
    conn = sensed_model.open_readonly(vault)
    projected = dict(conn.execute("SELECT path, sig FROM pages").fetchall())
    conn.close()
    assert projected == {path: seen[path] for path in paths}
    assert len(sf.edges(vault)) == 6


def test_a_rebuilt_projection_serves_nothing_until_every_page_is_projected(
    tmp_path: Path, monkeypatch
) -> None:
    """A projection rebuilt from empty, here after an upgrade from schema 2,
    never serves a partial status. Rebuilt a page per tick, the seal note used
    to read "refined by 1 later note" with complete evidence while the page
    that contradicts it was not projected yet."""
    sf.enable(monkeypatch)
    vault = tmp_path / "vault"
    # Named so the rebuild projects the seal note between its two partners.
    seal = f"{sf.KB}/Notes/Insights/b-seal.md"
    fx.write(vault, f"{sf.KB}/Notes/Insights/a-winter.md",
             sf.note("Winter", "2026-05-01", sf.WINTER_TEXT, links="On [[Notes/Insights/b-seal]]."))
    fx.write(vault, seal, sf.note("Seal wear", "2026-04-01", sf.SEAL_TEXT))
    fx.write(vault, f"{sf.KB}/Notes/Insights/c-denial.md",
             sf.note("Denial", "2026-05-02", sf.DENIAL_TEXT, links="On [[Notes/Insights/b-seal]]."))
    fx.seed(vault)
    fx.publish_graph(vault)
    sf.converge(vault, sf.StubInstrument(sf.default_table()))
    whole = sensed_model.status_for(vault, seal)
    assert whole["line"] == "refined by 1 later note; 1 open contradiction"
    conn = sqlite3.connect(sensed_model.projection_path(vault), isolation_level=None)
    conn.execute("UPDATE meta SET value='2' WHERE key='schema'")
    conn.close()
    one_page = dreamer.Budget(pages=1, cpu=120.0, wall=120.0)
    served = []
    for _tick in range(5):
        dreamer.run_once(vault, budget=one_page)
        served.append(sensed_model.status_for(vault, seal))
    assert all(status in (None, whole) for status in served), served
    assert served[-1] == whole


def test_a_replaced_ledger_is_reprojected(tmp_path: Path, monkeypatch) -> None:
    """LOW 9: the projection follows the ledger's identity, not only its length."""
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    stub = sf.StubInstrument(sf.default_table())
    sf.converge(vault, stub)
    path = sensing_ledger.ledger_path(vault)
    for suffix in ("", "-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    fresh = sensing_ledger.Ledger(vault)
    conn = fresh.connect()
    # The same number of rows as before, but none of them about these pairs.
    other = sf.StubInstrument(identity=sf.with_identity(model="stub/other"))
    for index in range(6):
        texts = sorted([f"x{index}", f"y{index}"], key=sensing.text_sha256)
        fresh.append(conn, [sensing.make_reading(
            sensing.PAIR_RELATION, other.identity,
            [sensing.InputUnit(f"u{t}", "p.md", sensing.text_sha256(t)) for t in texts],
            ab=sf.NEUTRAL, ba=sf.NEUTRAL, abstain_reason=None, sensed_at="t")])
    conn.close()
    sf.settle(vault)
    assert {row[5] for row in sf.edges(vault)} == {"pending"}
    assert sensed_model.status_for(vault, sf.SEAL) is None


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX modes")
def test_the_projection_is_private(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    assert sensed_model.projection_path(vault).stat().st_mode & 0o777 == 0o600
