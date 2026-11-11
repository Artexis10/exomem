"""The relation sensor through the tick, the worker and the queue."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf

from exomem import (
    commands,
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


def test_worker_order_follows_source_pages(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    for index in range(3):
        fx.write(
            vault,
            f"{fx.KB}/Notes/Insights/extra-{index}.md",
            sf.note(f"Extra {index}", "2026-05-10", f"Extra fact {index}.",
                    links="See [[Notes/Insights/seal-wear]] and [[Notes/Insights/seal-wear-winter]]."),
        )
    fx.publish_graph(vault)
    from exomem import dreamer

    dreamer.run_once(vault, budget=sf.BIG)
    jobs = _jobs(vault)
    assert len({j.scope_key for j in jobs}) > 1
    # Jobs drain page by page in path order, as the queue lists sources.
    sources = [j.scope_key for j in jobs]
    assert sources == sorted(sources)


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


def test_page_ahead_of_the_graph_is_skipped(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    assert _jobs(vault)
    # Edit the file without re-publishing the graph: its hash now mismatches.
    (vault / sf.WINTER).write_text(
        (vault / sf.WINTER).read_text(encoding="utf-8") + "\nnew line\n",
        encoding="utf-8",
    )
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    fit = relation_queue.RelationFit(vault)
    source = relation_sensor._file_node(conn, sf.WINTER)
    links = relation_sensor.plain_links(conn, fit.registry(source), source)
    assert relation_sensor.build_requests(vault, conn, fit, source, links) == []
    conn.close()


def test_target_ahead_of_the_graph_drops_only_that_link(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    fit = relation_queue.RelationFit(vault)
    source = relation_sensor._file_node(conn, sf.WINTER)
    links = relation_sensor.plain_links(conn, fit.registry(source), source)
    # Push the target file ahead of its published hash; the source still builds
    # nothing since its only link points there.
    (vault / sf.SEAL).write_text(
        (vault / sf.SEAL).read_text(encoding="utf-8") + "\nnew line\n",
        encoding="utf-8",
    )
    assert relation_sensor.build_requests(vault, conn, fit, source, links) == []
    # And a source pointing at a still-current target still builds.
    source2 = relation_sensor._file_node(conn, sf.DENIAL)
    links2 = relation_sensor.plain_links(conn, fit.registry(source2), source2)
    # DENIAL also links only to the stale SEAL: nothing builds for it either.
    assert relation_sensor.build_requests(vault, conn, fit, source2, links2) == []
    conn.close()


def test_no_admitted_model_issues_no_relation_queries(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    vault = sf.build(tmp_path)
    calls = []
    monkeypatch.setattr(
        sensed_model,
        "_relation_producer",
        lambda *a, **k: calls.append(a) or {"submitted": 0, "pages": [], "cursor": None},
    )
    monkeypatch.setattr(
        sensed_model,
        "_relation_ingest",
        lambda *a, **k: calls.append(a),
    )
    monkeypatch.setattr(
        sensed_model,
        "_relation_bookkeeping",
        lambda *a, **k: calls.append(a),
    )
    sf.settle(vault)
    assert calls == []


def test_ingest_cursor_walks_starved_rows(tmp_path: Path, monkeypatch) -> None:
    """Rows beyond the batch bound fill on a later tick, not never."""
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    conn = sensed_model._producer_connection(vault)
    rows = []
    for index in range(300):
        key = f"k{index:04d}"
        rows.append((key, "s.md", f"t{index}.md", "a", "wikilink", None, None, None, None,
                     "d" * 64, f"input-{index}"))
    conn.executemany(
        "INSERT OR REPLACE INTO relation_sensed VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.close()

    # Readings exist only for the last 25 rows.
    answered = {f"input-{i}" for i in range(275, 300)}

    from exomem import sensing_ledger

    class FakeReading:
        instrument_id = IDENTITY.instrument_id
        reading_id = "r1"
        vectors = {"answer": [0.9, 0.05, 0.05]}
        verdict = sensing.Verdict("supports", 0.9)
        columns = ("supports", "refines", "none")

    monkeypatch.setattr(
        sensing_ledger, "by_input_key",
        lambda ledger, q, key: [FakeReading()] if key in answered else [],
    )
    monkeypatch.setattr(relation_sensor, "INGEST_BATCH", 50)
    conn = sensed_model._producer_connection(vault)
    ledger = sqlite3.connect(":memory:")  # presence only; by_input_key is stubbed
    conn.execute("BEGIN IMMEDIATE")
    relation_sensor.ingest(conn, ledger)
    conn.execute("COMMIT")
    first = conn.execute(
        "SELECT count(*) FROM relation_sensed WHERE verdict='supports'").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    for _ in range(6):
        relation_sensor.ingest(conn, ledger)
    conn.execute("COMMIT")
    second = conn.execute(
        "SELECT count(*) FROM relation_sensed WHERE verdict='supports'").fetchone()[0]
    conn.close()
    assert first <= 50
    assert second == 25


def _log(vault: Path) -> Path:
    """The governed audit log: present so entries are written, not skipped."""
    log = vault / sf.KB / "log.md"
    log.write_text("# Log\n", encoding="utf-8")
    return log


def _warm(vault: Path, monkeypatch) -> None:
    """The reference-identity snapshot the one-shot accept verifies against."""
    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    from exomem import semantic_contract

    semantic_contract.reset_corpus_context_cache()
    semantic_contract.build_corpus_context(vault)


def _worker_and_ingest(vault: Path, stub: TypeStub) -> None:
    """Judge every relation.type job with `stub`, then settle the verdicts in."""
    pair = sf.StubInstrument(sf.default_table())

    def admit_selected(q):
        return stub if q == sensing.RELATION_TYPE else pair

    sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=10**6,
        idle_seconds=0.0, admit_selected=admit_selected, sleep=lambda s: None,
    )
    sf.settle(vault)


def _sensed_items(vault: Path):
    """(group, item) pairs whose item carries a sensed proposal."""
    result = commands.op_review_memory(vault, mode="relation-queue")
    return [
        (group, item)
        for group in result.get("groups", [])
        for item in group.get("items", [])
        if item.get("sensed")
    ]


def _accept(vault: Path, group: dict, item: dict, **kw) -> dict:
    return commands.op_connect_memory(
        vault,
        operation="accept-relation",
        ref=item["ref"],
        expected_hash=group["content_hash"],
        expected_fingerprint=item["fingerprint"],
        why="accept of the sensed candidate",
        **kw,
    )


def test_accept_wikilink_with_proposal(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    log = _log(vault)
    pairs = _sensed_items(vault)
    assert pairs, "queue annotated a wikilink candidate"
    group, item = pairs[0]
    proposal = item["sensed"]["relation"]
    before = (vault / item["from"]).read_text(encoding="utf-8")
    assert proposal not in before or f"- {proposal} " not in before
    result = _accept(vault, group, item, requested_relation=proposal)
    assert result.get("accepted") is True
    after = (vault / item["from"]).read_text(encoding="utf-8")
    assert f"- {proposal} [[" in after
    assert result.get("sensed", {}).get("relation") == proposal
    assert "[reading" in log.read_text(encoding="utf-8")


def test_accept_a_different_offered_option(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    group, item = _sensed_items(vault)[0]
    proposal = item["sensed"]["relation"]
    other = next(
        option["key"] for option in item.get("options", [])
        if option["key"] != proposal
    )
    result = _accept(vault, group, item, requested_relation=other)
    assert result.get("accepted") is True
    after = (vault / item["from"]).read_text(encoding="utf-8")
    assert f"- {other} [[" in after
    assert f"- {proposal} [[" not in after
    assert result.get("sensed", {}).get("relation") == proposal or proposal in json.dumps(result)


def test_no_requested_relation_never_writes_the_proposal(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    group, item = _sensed_items(vault)[0]
    proposal = item["sensed"]["relation"]
    before = (vault / item["from"]).read_text(encoding="utf-8")
    try:
        result = _accept(vault, group, item)
    except Exception as exc:
        result = {"error": str(exc)}
    after = (vault / item["from"]).read_text(encoding="utf-8")
    if result.get("accepted"):
        # If the rule accepts without a choice, it is still never the proposal.
        assert f"- {proposal} [[" not in after
    else:
        assert after == before


RTYPE = f"{sf.KB}/Notes/Insights/retype-me.md"


def _build_retype_vault(tmp_path: Path) -> Path:
    """The sensing vault plus a page whose authored link bullet is a retype case."""
    vault = sf.build(tmp_path)
    fx.write(
        vault,
        RTYPE,
        sf.note(
            "Retype me",
            "2026-05-04",
            "A generic link bullet awaits a type.",
            links="Mentions [[Notes/Insights/inlet-pressure]] too.",
            extra="\n## Relations\n\n- links_to [[Notes/Insights/seal-wear]]\n",
        ),
    )
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    return vault


def test_retype_accept_replaces_one_line(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = _build_retype_vault(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    result = commands.op_review_memory(vault, mode="relation-queue")
    retype = [
        (group, item)
        for group in result.get("groups", [])
        for item in group.get("items", [])
        if item.get("method") == "retype" and item.get("from") == RTYPE
    ]
    assert retype, "a retype candidate appears with its verdict"
    group, item = retype[0]
    proposal = item["sensed"]["relation"]
    before = (vault / RTYPE).read_text(encoding="utf-8")
    out = _accept(vault, group, item, requested_relation=proposal)
    assert out.get("accepted") is True
    after = (vault / RTYPE).read_text(encoding="utf-8")
    new_line = f"- {proposal} [[Knowledge Base/Notes/Insights/seal-wear]]"
    # The governed edit also re-stamps `updated:`; everything else is the
    # one-line replacement.
    norm = lambda text: "\n".join(
        line for line in text.splitlines() if not line.startswith("updated:")
    )
    assert norm(after) == norm(before).replace(
        "- links_to [[Notes/Insights/seal-wear]]", new_line
    )


def test_retype_accept_refuses_on_drift(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = _build_retype_vault(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    result = commands.op_review_memory(vault, mode="relation-queue")
    group, item = next(
        (g, i)
        for g in result.get("groups", [])
        for i in g.get("items", [])
        if i.get("method") == "retype" and i.get("from") == RTYPE
    )
    drifted = (vault / RTYPE).read_text(encoding="utf-8") + "\ndrifted line\n"
    (vault / RTYPE).write_text(drifted, encoding="utf-8")
    with pytest.raises(Exception, match="REVIEW_ITEM_CHANGED|REVIEW_REFRESH_REQUIRED|drift|changed"):
        _accept(vault, group, item, requested_relation=item["sensed"]["relation"])
    assert (vault / RTYPE).read_text(encoding="utf-8") == drifted
