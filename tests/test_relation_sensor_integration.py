"""The relation sensor through the tick, the worker and the queue."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf

from exomem import (
    cli_ops,
    commands,
    epistemic_graph,
    freshness,
    relation_queue,
    relation_registry,
    relation_sensor,
    sensed_model,
    sensing,
    sensing_ledger,
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
    assert item["sensed"]["relation"] == "supports"
    assert item["sensed"]["proposed"] is True
    assert item["sensed"]["reading_id"]
    assert item.get("options"), "the item carries the menu as options"


def test_ready_verdicts_fill_when_the_producer_spends_the_remaining_budget(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    assert _rows(vault) and all(row[7] is None for row in _rows(vault))
    relation = TypeStub()
    pair = sf.StubInstrument(sf.default_table())
    sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=10**6,
        idle_seconds=0.0,
        admit_selected=lambda question: relation if question == sensing.RELATION_TYPE else pair,
        sleep=lambda seconds: None,
    )
    conn = sensed_model.open_readonly(vault)
    seen = dict(conn.execute("SELECT path, sig FROM pages"))
    conn.close()
    original = relation_sensor.produce
    spent = False

    def spend_remaining_budget(*args, **kwargs):
        nonlocal spent
        result = original(*args, **kwargs)
        spent = True
        return result

    monkeypatch.setattr(relation_sensor, "produce", spend_remaining_budget)
    sensed_model.run_tick(vault, seen=seen, halt=lambda: "cpu" if spent else None)
    assert spent
    assert all(row[7] == "supports" for row in _rows(vault))


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


def _append_graph_rows(conn, table, changes) -> None:
    cursor = conn.execute(f"SELECT * FROM {table} LIMIT 1")
    template = dict(zip((column[0] for column in cursor.description), cursor.fetchone(), strict=True))
    conn.executemany(
        f"INSERT INTO {table} ({','.join(template)}) VALUES ({','.join('?' for _ in template)})",
        (tuple((template | change).values()) for change in changes),
    )


def test_high_degree_page_fills_after_specific_pairs_and_keeps_bullet_evidence(tmp_path: Path, record_property) -> None:
    vault = sf.build(tmp_path)
    snapshot = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    conn = sqlite3.connect(":memory:")
    snapshot.backup(conn)
    snapshot.close()
    targets = [f"{fx.KB}/Notes/Insights/target-{index:04d}.md" for index in range(514)]
    _append_graph_rows(conn, "graph_nodes", (
        {"node_key": f"file:{target}", "kind": "file", "path": target}
        for target in targets
    ))
    conn.execute("DELETE FROM graph_edges WHERE source_path=?", (sf.WINTER,))
    edges = []
    for index, target in enumerate(targets[:512]):
        edges.append({
            "edge_key": f"wiki-{index}", "src_key": f"file:{sf.WINTER}",
            "source_path": sf.WINTER, "dst_key": f"file:{target}",
            "dst_page_key": f"file:{target}", "origin": "wikilink",
            "review_evidence": json.dumps({"internal": {"occurrence": index, "line": 1}}),
        })
        if index < 504:
            edges.append({
                "edge_key": f"typed-{index}", "src_key": f"file:{sf.WINTER}",
                "source_path": sf.WINTER, "dst_key": f"file:{target}",
                "dst_page_key": f"file:{target}", "origin": "markdown_relation",
                "raw_relation": "typed_alias", "source_anchor": f"line-{index + 1}",
            })
    bullets = ["- untyped_alias [[target-0512]]", "- relates_to [[target-0513]]"]
    for index, target in enumerate(targets[512:]):
        edges.append({
            "edge_key": f"bullet-{index}", "src_key": f"file:{sf.WINTER}",
            "source_path": sf.WINTER, "dst_key": f"file:{target}",
            "dst_page_key": f"file:{target}", "origin": "markdown_relation",
            "raw_relation": "untyped_alias" if index == 0 else "relates_to",
            "source_anchor": f"line-{600 - index}", "metadata": json.dumps({"line": bullets[index]}),
        })
    _append_graph_rows(conn, "graph_edges", edges)
    registry = replace(
        relation_registry.core_registry(),
        aliases={"untyped_alias": "links_to", "typed_alias": "vault.reference"},
        extensions={"vault.reference": relation_registry.RelationDefinition(
            key="vault.reference", parent="links_to", description="A specific authored reference.",
            family=relation_registry.core_registry().core["links_to"].family,
        )},
    )
    source = relation_sensor._file_node(conn, sf.WINTER)
    statements = []
    conn.set_trace_callback(statements.append)
    links = relation_sensor.plain_links(conn, registry, source)
    assert [link.target for link in links] == targets[504:512]
    assert len(statements) <= 2, "candidate selection must not query once per outgoing link"

    conn.set_trace_callback(None)
    conn.execute("DELETE FROM graph_edges WHERE edge_key IN ('wiki-508', 'wiki-509', 'wiki-510', 'wiki-511')")
    links = relation_sensor.plain_links(conn, registry, source)
    assert [link.target for link in links] == [*targets[504:508], targets[513], targets[512]]
    assert [(link.anchor, link.bullet) for link in links[4:]] == [
        ("line-599", bullets[1]), ("line-600", bullets[0]),
    ]
    rows = {
        relation_sensor.request_key(sf.WINTER, link.target, link.anchor): {
            "source_path": sf.WINTER, "target_path": link.target, "anchor": link.anchor,
            "kind": "retype", "verdict": "supports",
        }
        for link in links[4:]
    }
    statements.clear()
    progress = []
    conn.set_trace_callback(statements.append)
    conn.set_progress_handler(lambda: progress.append(1) or 0, 1)
    candidates = relation_sensor.retype_candidates_many(conn, {sf.WINTER: registry}, rows)
    conn.set_progress_handler(None, 0)
    assert len(statements) == 1, "the queue must fetch all selected bullets in one query"
    assert {item["to"]: item["evidence"]["line"] for item in candidates[sf.WINTER]} == {
        targets[512]: bullets[0], targets[513]: bullets[1],
    }
    conn.set_trace_callback(None)
    record_property("retype_query_plan", [
        row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + statements[0])
    ])
    indexed_steps = len(progress)
    record_property("indexed_retype_graph_vm_steps", indexed_steps)
    assert indexed_steps <= 500, "two exact anchors must not visit every authored bullet"

    # An older read-only publication can lack the additive index until its writer initializes.
    conn.execute("DROP INDEX idx_graph_edges_relation_anchor")
    progress.clear()
    conn.set_progress_handler(lambda: progress.append(1) or 0, 1)
    assert relation_sensor.retype_candidates_many(conn, {sf.WINTER: registry}, rows) == candidates
    conn.set_progress_handler(None, 0)
    record_property("unindexed_retype_graph_vm_steps", len(progress))
    assert len(progress) > indexed_steps
    conn.close()


def test_producer_selection_work_stays_bounded_at_50000_graph_nodes(tmp_path: Path, monkeypatch, record_property) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    for index in range(8):
        fx.write(vault, f"{fx.KB}/Notes/Insights/extra-{index}.md", sf.note(
            f"Extra {index}", "2026-05-10", f"Extra fact {index}.",
            links="See [[Notes/Insights/seal-wear]].",
        ))
    fx.publish_graph(vault)
    sf.settle(vault)
    snapshot = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    conn = sqlite3.connect(":memory:")
    snapshot.backup(conn)
    snapshot.close()

    def measured_tick():
        statements = []
        progress = []
        conn.set_trace_callback(statements.append)
        conn.set_progress_handler(lambda: progress.append(1) or 0, 100)
        try:
            result = relation_sensor.produce(vault, conn, cursor="", changed=[])
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_trace_callback(None)
        return result, statements, len(progress) * 100

    small, small_statements, small_work = measured_tick()
    node_count = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    generated_count = 50_000 - node_count
    paths = [f"{fx.KB}/Notes/Insights/zz-generated-{index:05d}.md" for index in range(generated_count // 2)]
    _append_graph_rows(conn, "graph_nodes", (
        {"node_key": f"file:{path}", "kind": "file", "path": path} for path in paths
    ))
    _append_graph_rows(conn, "graph_edges", (
        {"edge_key": f"generated-{index}", "src_key": f"file:{path}",
         "source_path": path, "origin": "wikilink"}
        for index, path in enumerate(paths)
    ))
    selected_paths = [path for path, _ in small["pages"]]
    _append_graph_rows(conn, "graph_nodes", (
        {"node_key": f"unit:dense-{index}", "kind": "candidate",
         "path": selected_paths[index % len(selected_paths)], "title": "Generated fragment",
         "line_start": 1, "line_end": 1}
        for index in range(generated_count - len(paths))
    ))
    assert conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0] == 50_000
    large, large_statements, large_work = measured_tick()
    cursor_sql = next(sql for sql in large_statements if sql.startswith("SELECT path FROM graph_nodes"))
    source_sql = next(sql for sql in large_statements if sql.startswith("SELECT path, title, page_type, metadata, source_hash"))
    record_property("cursor_plan", [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + cursor_sql)])
    record_property("source_plan", [row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + source_sql)])
    progress = []
    conn.set_progress_handler(lambda: progress.append(1) or 0, 1)
    conn.execute(cursor_sql).fetchall()
    conn.set_progress_handler(None, 0)
    cursor_steps = len(progress)
    record_property("cursor_vm_steps", cursor_steps)
    assert cursor_steps <= 200, "cursor selection must not visit candidate nodes sharing selected paths"
    record_property("small_graph_vm_steps", small_work)
    record_property("large_graph_vm_steps", large_work)
    record_property("small_graph_queries", len(small_statements))
    record_property("large_graph_queries", len(large_statements))
    assert len(large["pages"]) == relation_sensor.PAGES_PER_TICK
    assert large["pages"] == small["pages"]
    assert large["submitted"] == small["submitted"] > 0
    assert len(large_statements) == len(small_statements)
    assert large_work <= small_work * 1.2 + 500, (small_work, large_work)
    conn.close()


@pytest.mark.parametrize("page_heading", (True, False))
def test_excerpt_preserves_prose_and_omits_only_authored_relations(tmp_path: Path, page_heading) -> None:
    vault = sf.build(tmp_path)
    path = f"{fx.KB}/Notes/Insights/excerpt.md"
    text = sf.note(
        "A", "2026-05-10", "Another observed fact.",
        links="Another fact uses [[Notes/Insights/seal-wear#Details|the alias]].\n"
        "- see [[Notes/Insights/seal-wear]]\n"
        "- SUPPORTS [[Notes/Insights/inlet-pressure]]\n"
        "- cites-alias [[Notes/Insights/inlet-pressure]]\n"
        "```\n- supports [[literal]]\n```",
        extra="\n## Relations_\n\nRelations-only context.\n- links_to [[Notes/Insights/seal-wear]]\n"
        "### More relation details\nHidden relation detail.\n\n## Evidence\nKept after relations.\n",
    )
    if not page_heading:
        text = text.replace("# A\n\n", "")
    fx.write(vault, path, text)
    fx.write(vault, f"{fx.KB}/_Schema/relation-registry.yaml", (
        "schema_version: 1\nextensions:\n  vault.cites:\n"
        "    parent: relates_to\n    description: Cites a specific linked statement.\n"
        "    aliases: [cites_alias]\n    direction: directed\n"
    ))
    fx.publish_graph(vault)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    page = relation_sensor._file_node(conn, path)
    fit = relation_queue.RelationFit(vault)
    links = relation_sensor.plain_links(conn, fit.registry(page), page)
    source_text = relation_sensor.build_requests(vault, conn, fit, page, links)[0][1].texts[0]
    conn.close()
    assert source_text.startswith("A\nAnother fact uses the alias.")
    assert "- see seal-wear" in source_text
    assert "- supports [[literal]]" in source_text
    assert "SUPPORTS" not in source_text
    assert "cites-alias" not in source_text
    assert "Relations-only context" not in source_text
    assert "Hidden relation detail" not in source_text
    assert "## Evidence Kept after relations." in source_text


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
    queued = list(sensed_model.runnable_jobs(vault))
    pair_positions = [i for i, job in enumerate(queued) if job.request.question_type == sensing.PAIR_RELATION]
    relation_positions = [i for i, job in enumerate(queued) if job.request.question_type == sensing.RELATION_TYPE]
    assert pair_positions and relation_positions
    assert max(pair_positions) < min(relation_positions)


def test_unchanged_scope_keeps_its_verdict_and_changed_input_clears_it(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    before = [row for row in _rows(vault) if row[1] == sf.WINTER]
    assert before and before[0][7] == "supports"
    fit = relation_queue.RelationFit(vault)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    source = relation_sensor._file_node(conn, sf.WINTER)
    assert relation_sensor.page_scope(vault, conn, fit, source).state == "submitted"
    conn.close()
    assert [row for row in _rows(vault) if row[1] == sf.WINTER] == before
    assert relation_sensor.sensed_rows_for(vault, [sf.WINTER])

    fx.edit(vault, sf.WINTER, (vault / sf.WINTER).read_text().replace("Builds on", "The evidence follows"))
    fx.publish_graph(vault)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    source = relation_sensor._file_node(conn, sf.WINTER)
    assert relation_sensor.page_scope(vault, conn, relation_queue.RelationFit(vault), source).state == "submitted"
    conn.close()
    changed = [row for row in _rows(vault) if row[1] == sf.WINTER]
    assert changed and changed[0][5:9] == (None, None, None, None)
    assert relation_sensor.sensed_rows_for(vault, [sf.WINTER]) == {}


def test_changed_priority_never_skips_cursor_pages_or_duplicates_a_scope(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    paths = {name: f"{fx.KB}/Notes/Insights/{name}.md" for name in [
        *(f"a-{i}" for i in range(4)), *(f"m-{i}" for i in range(8)), "z-0",
    ]}
    for name, path in paths.items():
        fx.write(vault, path, sf.note(name, "2026-05-10", f"{name} fact."))
    fx.publish_graph(vault)
    sf.settle(vault)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    start = paths["a-0"]
    changed = [paths[f"m-{i}"] for i in range(8)]
    result = relation_sensor.produce(vault, conn, cursor=start, changed=changed)
    assert result["cursor"] == start
    assert [path for path, _ in result["pages"]] == changed
    result = relation_sensor.produce(vault, conn, cursor=start, changed=[paths["a-1"], paths["m-0"], paths["a-1"]])
    expected = [paths["a-1"], paths["m-0"], paths["a-2"], paths["a-3"], sf.INLET, paths["m-1"], paths["m-2"], paths["m-3"]]
    assert [path for path, _ in result["pages"]] == expected
    assert result["cursor"] == paths["m-3"]
    result = relation_sensor.produce(vault, conn, cursor=paths["z-0"], changed=[])
    assert [path for path, _ in result["pages"]] == [paths["a-0"], paths["a-1"], paths["a-2"], paths["a-3"], sf.INLET, paths["m-0"], paths["m-1"], paths["m-2"]]
    assert result["cursor"] == paths["m-2"]
    conn.close()


def test_replaced_ledger_clears_relation_reading_citations(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    before = _rows(vault)
    assert before and all(row[5] for row in before)
    assert relation_sensor.sensed_rows_for(vault, [sf.WINTER])

    sensing_ledger.ledger_path(vault).unlink()
    replacement = sensing_ledger.Ledger(vault).connect()
    replacement.close()
    sf.settle(vault, rounds=1)
    after = _rows(vault)
    assert after and all(row[5:9] == (None, None, None, None) for row in after)
    assert relation_sensor.sensed_rows_for(vault, [sf.WINTER]) == {}


@pytest.mark.parametrize("immediate", (True, False))
def test_foreground_halt_stops_relation_scopes_and_preserves_cursor(tmp_path: Path, monkeypatch, immediate) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    conn = epistemic_graph.EpistemicGraphIndex(vault)._open_read_snapshot()
    requested = immediate
    original = relation_sensor.page_scope

    def become_active_after_scope(*args, **kwargs):
        nonlocal requested
        result = original(*args, **kwargs)
        requested = True
        return result

    monkeypatch.setattr(relation_sensor, "page_scope", become_active_after_scope)
    result = relation_sensor.produce(vault, conn, cursor="", changed=[], halt=lambda: "foreground" if requested else None)
    assert len(result["pages"]) == (0 if immediate else 1)
    assert result["stop"] == "foreground"
    assert result["cursor"] == ("" if immediate else sf.INLET)
    conn.close()


@pytest.mark.parametrize("line, expected", [
    ("2026 budget follows [[X]]", "2026 budget follows X"),
    ("1.5x faster than [[X]]", "1.5x faster than X"),
    ("+ plus bullet [[X]]", "plus bullet X"),
    ("2. ordered [[X]]", "ordered X"),
    ("3) ordered [[X]]", "ordered X"),
])
def test_link_sentence_preserves_prose_and_removes_list_markers(line, expected) -> None:
    assert relation_sensor.link_line([line], 1) == expected


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
    template = _jobs(vault)[0].request
    conn = sensed_model._producer_connection(vault)
    rows = []
    readings = []
    for index in range(300):
        key = f"k{index:04d}"
        source_text = f"A distinct source statement {index}."
        request = sensing.SenseRequest(
            template.question_type, template.input_version,
            (replace(template.inputs[0], text_sha256=sensing.text_sha256(source_text)), *template.inputs[1:]),
            (source_text, *template.texts[1:]),
        )
        rows.append((key, "s.md", f"t{index}.md", "a", "wikilink", None, None, None, None,
                     "d" * 64, request.input_key, "source-hash", "target-hash"))
        if index >= 275:
            columns = sensing_llm.menu_columns(request)[1]
            vector = [0.01] * len(columns)
            vector[columns.index("supports")] = 0.9
            vector[-1] += 1 - sum(vector)
            readings.append(sensing.make_request_reading(
                request, IDENTITY, vectors={"answer": vector},
                abstain_reason=None, sensed_at="2026-05-10T00:00:00Z",
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO relation_sensed VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.close()

    # Real ledger readings exist only for the last 25 rows.
    ledger_store = sensing_ledger.Ledger(vault)
    ledger = ledger_store.connect()
    assert all(readings)
    ledger_store.append(ledger, readings)
    monkeypatch.setattr(relation_sensor, "INGEST_BATCH", 50)
    conn = sensed_model._producer_connection(vault)
    queries = []
    ledger.set_trace_callback(queries.append)
    conn.execute("BEGIN IMMEDIATE")
    relation_sensor.ingest(conn, ledger)
    conn.execute("COMMIT")
    assert len(queries) == 1, "one batch must not query once per pending row"
    first = conn.execute(
        "SELECT count(*) FROM relation_sensed WHERE verdict='supports'").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    for _ in range(6):
        queries.clear()
        relation_sensor.ingest(conn, ledger)
        assert len(queries) == 1
    conn.execute("COMMIT")
    second = conn.execute(
        "SELECT count(*) FROM relation_sensed WHERE verdict='supports'").fetchone()[0]
    conn.close()
    ledger.close()
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
    assert result.get("sensed", {}).get("relation") == proposal


def test_inactive_instrument_hides_persisted_proposal_before_next_tick(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    group, item = _sensed_items(vault)[0]
    before = (vault / item["from"]).read_text(encoding="utf-8")
    assert relation_sensor.sensed_rows_for(vault, [item["from"]])

    monkeypatch.setattr(sensing_llm, "active_instruments", lambda: {})
    assert _sensed_items(vault) == []
    assert relation_sensor.sensed_rows_for(vault, [item["from"]]) == {}
    with pytest.raises(cli_ops.OpError, match="VOCABULARY_APPLICATION_INVALID"):
        _accept(vault, group, item, requested_relation=item["sensed"]["relation"])
    assert (vault / item["from"]).read_text(encoding="utf-8") == before


def test_target_edit_hides_old_reading_without_resensing_source(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    group, item = next((g, i) for g, i in _sensed_items(vault) if i["to"] == sf.SEAL)
    before = (vault / item["from"]).read_text(encoding="utf-8")
    old_rows = _rows(vault)
    fx.edit(vault, sf.SEAL, (vault / sf.SEAL).read_text(encoding="utf-8").replace(
        sf.SEAL_TEXT, "Seal wear stays constant after a dry start.",
    ))
    fx.publish_graph(vault)
    _warm(vault, monkeypatch)
    assert _rows(vault) == old_rows, "the reading stays persisted until a later sensor tick"
    assert relation_sensor.sensed_for_candidate(vault, item["from"], sf.SEAL) is None
    assert not any(i["to"] == sf.SEAL for _, i in _sensed_items(vault))
    with pytest.raises(cli_ops.OpError, match="VOCABULARY_APPLICATION_INVALID"):
        _accept(vault, group, item, requested_relation=item["sensed"]["relation"])
    assert (vault / item["from"]).read_text(encoding="utf-8") == before


def test_changed_registry_menu_cannot_widen_a_stale_accept(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch)
    _active(monkeypatch)
    vault = sf.build(tmp_path)
    sf.settle(vault)
    _worker_and_ingest(vault, TypeStub())
    _warm(vault, monkeypatch)
    group, item = _sensed_items(vault)[0]
    before = (vault / item["from"]).read_text(encoding="utf-8")
    fx.write(vault, f"{fx.KB}/_Schema/relation-registry.yaml", (
        "schema_version: 1\nextensions:\n  vault.applies_to:\n"
        "    parent: relates_to\n    description: Applies within the linked note's conditions.\n"
        "    direction: directed\n"
    ))
    fx.publish_graph(vault)
    _warm(vault, monkeypatch)
    assert _sensed_items(vault) == []
    with pytest.raises(cli_ops.OpError, match="VOCABULARY_APPLICATION_INVALID"):
        _accept(vault, group, item, requested_relation=item["sensed"]["relation"])
    assert (vault / item["from"]).read_text(encoding="utf-8") == before


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
    def norm(text):
        return "\n".join(line for line in text.splitlines() if not line.startswith("updated:"))
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
    with pytest.raises(ValueError, match="STALE_EDIT"):
        _accept(vault, group, item, requested_relation=item["sensed"]["relation"])
    assert (vault / RTYPE).read_text(encoding="utf-8") == drifted
