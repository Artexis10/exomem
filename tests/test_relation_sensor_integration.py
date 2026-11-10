"""The relation sensor through the tick, the worker and the queue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf

from exomem import (
    epistemic_graph,
    freshness,
    relation_queue,
    relation_registry,
    relation_sensor,
    sensed_model,
    sensing,
    sensing_llm,
    sensor_worker,
)

IDENTITY = sensing.InstrumentIdentity(
    model="stub/sensor",
    revision="1",
    weights_sha256="0" * 64,
    runtime="stub",
    runtime_version="1",
    template_version="relation-type-v1",
    label_map_version="relation-type-v1",
    fixture_set="relation-type-v1-judged",
    label_token_map="x" * 64,
)


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    yield
    freshness.clear()


class TypeStub:
    """A relation.type instrument: answers `supports` at 0.9 on every request."""

    def __init__(self, label_index: int = 0):
        self.identity = IDENTITY
        self.calls = 0
        self._index = label_index

    def judge_requests(self, requests):
        self.calls += len(requests)
        out = []
        for request in requests:
            _entries, columns = sensing_llm.menu_columns(request)
            vector = [0.05] * len(columns)
            vector[self._index] = 0.9
            rest = 1.0 - 0.9 - 0.05 * (len(columns) - 1)
            vector[-1] += rest
            out.append({"vectors": {"answer": vector}, "abstain_reason": None})
        return out


def _admit(stub: TypeStub):
    return lambda _q: stub


def _active(monkeypatch, *stubs: TypeStub) -> None:
    monkeypatch.setattr(
        sensing_llm,
        "active_instruments",
        lambda: {s.identity.instrument_id: s.identity for s in (stubs or (TypeStub(),))},
    )


def _queue(vault: Path) -> dict:
    return epistemic_graph.EpistemicGraphIndex(vault).relation_review_batch()


def _rows(vault: Path) -> list[tuple]:
    conn = sensed_model.open_readonly(vault)
    if conn is None:
        return []
    try:
        try:
            return conn.execute(
                "SELECT request_key, source_path, target_path, anchor, kind, "
                "reading_id, instrument_id, verdict, p FROM relation_sensed"
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        conn.close()


def _jobs(vault: Path) -> list:
    conn = sensed_model.open_readonly(vault)
    if conn is None:
        return []
    try:
        return [j for j in sensed_model.iter_jobs(conn) if j.request.question_type == sensing.RELATION_TYPE]
    finally:
        conn.close()


def test_silence_without_an_admitted_model(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    # The empty pin registry admits nothing: no jobs, no rows, no queue change.
    assert _jobs(vault) == []
    assert _rows(vault) == []


def test_queue_byte_identical_without_readings(tmp_path: Path, monkeypatch) -> None:
    vault = sf.build(tmp_path)
    before = _queue(vault)
    sf.enable(monkeypatch)
    sf.settle(vault)
    assert _queue(vault) == before


def test_retire_then_ingest_keeps_the_proposal(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    jobs = _jobs(vault)
    assert jobs, "producer submitted relation.type requests"
    stub = TypeStub()
    pair = sf.StubInstrument(sf.default_table())

    def admit_selected(q):
        return stub if q == sensing.RELATION_TYPE else pair

    sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=10**6,
        idle_seconds=0.0, admit_selected=admit_selected, sleep=lambda s: None,
    )
    # Jobs retire as satisfied; the projection rows keep their verdicts.
    sf.settle(vault)
    rows = [r for r in _rows(vault) if r[7] == "supports"]
    assert rows, "verdicts projected even though the jobs retired"
    queue = _queue(vault)
    sensed = [
        item
        for group in queue.get("groups", [])
        for item in group.get("items", [])
        if item.get("sensed")
    ]
    assert sensed, "the queue annotates the proposal"
    item = sensed[0]
    assert item["sensed"]["relation"] == "supports" or item["sensed"]["relation"]
    assert item["sensed"]["proposed"] is True
    assert item["sensed"]["reading_id"]
    assert item.get("options"), "the item carries the menu as options"


def test_menu_equals_the_queues_options(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    index = epistemic_graph.EpistemicGraphIndex(vault)
    conn = index._open_read_snapshot()
    fit = relation_queue.RelationFit(vault)
    source_page = relation_queue._page_for(vault, sf.WINTER)
    registry = fit.registry(source_page)
    source = relation_sensor._file_node(conn, sf.WINTER)
    target = relation_sensor._file_node(conn, sf.SEAL)
    assert source and target
    candidate = {"from": sf.WINTER, "to": sf.SEAL, "relation_type": "links_to",
                 "method": "wikilink"}
    menu = relation_sensor.menu_for(fit, registry, source, target, candidate)
    queue_options = fit.options(
        candidate, registry=registry,
        relation=relation_registry.generic_relation_key(),
    )
    conn.close()
    assert [o["key"] for o in menu] == [o["key"] for o in queue_options]


def test_producer_bounds_pages_per_tick(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    # Add pages beyond the per-tick bound, each linking the seal page.
    for index in range(relation_sensor.PAGES_PER_TICK + 4):
        fx.write(
            vault,
            f"{fx.KB}/Notes/Insights/extra-{index}.md",
            sf.note(f"Extra {index}", "2026-05-10", f"Extra fact {index}.",
                    links="See [[Notes/Insights/seal-wear]]."),
        )
    fx.publish_graph(vault)
    from exomem import dreamer

    dreamer.run_once(vault, budget=sf.BIG)
    jobs = _jobs(vault)
    pages = {j.scope_key for j in jobs}
    assert len(pages) <= relation_sensor.PAGES_PER_TICK
    assert all(
        len([j for j in jobs if j.scope_key == s]) <= relation_sensor.LINKS_PER_PAGE
        for s in pages
    )


def test_edited_page_resubmits_and_stale_rows_drop(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    before = {row[1:4] for row in _rows(vault)}
    assert any(row[1] == sf.WINTER for row in _rows(vault))
    # Remove the wikilink line: the stale request's row must leave.
    text = (vault / sf.WINTER).read_text(encoding="utf-8")
    fx.edit(vault, sf.WINTER, text.replace(
        "Builds on [[Notes/Insights/seal-wear]].", "Builds on earlier notes."
    ))
    sf.settle(vault)
    after = {row[1:4] for row in _rows(vault)}
    assert before - after, "the edited page's stale rows dropped"


def test_question_scoped_refusal_keeps_pair_relation(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    stub = sf.StubInstrument(sf.default_table())
    # Admit pair.relation's stub; relation.type hits the real pin registry.
    def admit_selected(q):
        if q == sensing.PAIR_RELATION:
            return stub
        return sensor_worker.admit_question(vault, q)

    sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=10**6,
        idle_seconds=0.0, admit_selected=admit_selected, sleep=lambda s: None,
    )
    status = json.loads(
        sensor_worker._state_file(vault, sensor_worker.STATUS_FILE).read_text()
    )
    refusal = status["refusals"].get(sensing.RELATION_TYPE)
    assert refusal and refusal["reason"] == "no-admitted-sensor-model"
    assert sensing.PAIR_RELATION not in (status.get("refusals") or {})
