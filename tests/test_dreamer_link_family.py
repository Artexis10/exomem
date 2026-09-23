"""D1-T8: the link family, `upkeep_link`.

Link proposals come from the relation queue's own per-page, embedding-free
generator and keep only four structural methods. They reuse the relation
queue's identity, ref and fingerprint, so the relation namespace holds exactly
one triage record per link.
"""

from __future__ import annotations

import time
from pathlib import Path

import dreamer_fixture as fx
import pytest

from exomem import (
    corpus_aware,
    dreamer,
    dreamer_families,
    dreamer_store,
    epistemic_graph,
    freshness,
    relation_queue,
)

LATER = time.time() + 3 * 3600


@pytest.fixture(autouse=True)
def _clean():
    freshness.clear()
    dreamer.reset_for_tests()
    dreamer_store.clear_reader_memo()
    yield
    dreamer.reset_for_tests()
    freshness.clear()
    dreamer_store.clear_reader_memo()


def _rows(vault: Path, *, state: str | None = "open") -> list[dict]:
    conn = dreamer_store.DreamerStore(vault).connect()
    try:
        rows = [
            dreamer_store._row_dict(row)
            for row in _all(conn)
            if row["family"] == dreamer_families.LINK_FAMILY
            and (state is None or row["state"] == state)
        ]
    finally:
        conn.close()
    return rows


def _all(conn):
    import sqlite3

    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM candidates ORDER BY id").fetchall()
    finally:
        conn.row_factory = None


def _pair(rows: list[dict], subject: str, to: str) -> dict | None:
    return next(
        (
            row
            for row in rows
            if row["subject_path"] == subject and row["measures"].get("to") == to
        ),
        None,
    )


def _quiet(vault: Path, *, now: float | None = None) -> None:
    results = fx.run_to_quiet(vault, now=now)
    assert all(result.stop_reason != "error" for result in results), results


def _settled(vault: Path) -> None:
    """Detect now, then tick again once the evidence has been stable an hour."""
    _quiet(vault)
    _quiet(vault, now=LATER)


def test_shared_source_pair_becomes_a_relation_ref_with_source_path(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    rows = _rows(vault)
    forward = _pair(rows, fx.CAVITATION, fx.INLET)
    assert forward is not None
    assert forward["kind"] == dreamer_families.LINK_KIND
    assert forward["measures"]["method"] == "shared_sources"
    assert forward["ref"].startswith(relation_queue.RELATION_REVIEW_PREFIX)
    assert forward["id"] == relation_queue.parse_relation_review_ref(forward["ref"])
    route = forward["route"]
    assert route["tool"] == "connect_memory"
    assert route["args"]["operation"] == "accept-relation"
    assert route["args"]["ref"] == forward["ref"]
    assert route["args"]["path"] == fx.CAVITATION
    assert route["args"]["expected_fingerprint"] == forward["fingerprint"]
    evidence = {item["path"]: item for item in forward["evidence"]}
    assert set(evidence) == {fx.CAVITATION, fx.INLET, fx.SOURCE_ONE}
    assert all(item["sig"] for item in evidence.values())
    # The relation namespace resolves the same identity from the source-path hint.
    resolved = relation_queue.resolve_candidate(vault, forward["ref"], source_path=fx.CAVITATION)
    assert resolved.fingerprint == forward["fingerprint"]
    assert _pair(rows, fx.INLET, fx.CAVITATION) is not None


def test_authored_edge_either_direction_resolves(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    assert _pair(_rows(vault), fx.CAVITATION, fx.INLET) is not None
    fx.edit(
        vault,
        fx.INLET,
        fx.insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-03",
            observation="Inlet pressure falls before cavitation begins.",
            extra="\n## Relations\n\n- relates_to [[Notes/Insights/pump-cavitation]]\n",
        ),
    )
    _quiet(vault)
    open_rows = _rows(vault)
    assert _pair(open_rows, fx.CAVITATION, fx.INLET) is None
    assert _pair(open_rows, fx.INLET, fx.CAVITATION) is None
    resolved = _rows(vault, state="resolved")
    assert _pair(resolved, fx.CAVITATION, fx.INLET) is not None


def test_wikilink_only_and_frontmatter_source_yield_nothing(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    rows = _rows(vault, state=None)
    assert rows
    assert {row["measures"]["method"] for row in rows} <= dreamer_families.LINK_METHODS
    targets = {row["measures"]["to"] for row in rows}
    assert fx.ENTITY not in targets
    assert fx.SOURCE_ONE not in targets and fx.SOURCE_TWO not in targets
    # The seal-wear note shares nothing structural with anyone: no proposal.
    assert not any(row["subject_path"] == fx.SEAL_WEAR for row in rows)


def test_embedding_proximity_is_unreachable(tmp_path: Path, monkeypatch) -> None:
    vault = fx.build(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("embedding proximity must be unreachable from upkeep")

    monkeypatch.setattr(epistemic_graph, "_embedding_proximity_candidates", forbidden)
    monkeypatch.setattr(corpus_aware, "_best_cosine_per_file", forbidden)
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    _quiet(vault)
    assert all(row["measures"]["method"] != "embedding_proximity" for row in _rows(vault))


def test_a_relation_dismissed_in_the_queue_is_not_deliverable(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _settled(vault)
    forward = _pair(_rows(vault), fx.CAVITATION, fx.INLET)
    assert forward["deliverable"] is True
    relation_queue.triage(
        vault,
        ref=forward["ref"],
        action="dismiss",
        why="false_positive: unrelated",
        source_path=fx.CAVITATION,
    )
    _quiet(vault, now=LATER + 60)
    after = _pair(_rows(vault), fx.CAVITATION, fx.INLET)
    assert after is not None and after["state"] == "open"
    assert after["fingerprint"] == forward["fingerprint"]
    assert after["deliverable"] is False


def test_unchanged_rerun_adds_no_row(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _settled(vault)
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    generation = store.generation(conn)
    before = {row["id"]: row["fingerprint"] for row in _rows(vault, state=None)}
    idle = dreamer.run_once(vault, clock=dreamer.Clock(time=lambda: LATER))
    assert idle.stop_reason == "idle"
    assert store.generation(conn) == generation
    # A same-content rewrite is processed again and changes nothing but the settle clock.
    fx.edit(vault, fx.CAVITATION, (vault / fx.CAVITATION).read_text("utf-8"), graph=False)
    _quiet(vault, now=LATER)
    after = {row["id"]: row["fingerprint"] for row in _rows(vault, state=None)}
    assert after == before
    conn.close()


def test_evidence_change_refreshes_the_fingerprint(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _settled(vault)
    forward = _pair(_rows(vault), fx.CAVITATION, fx.INLET)
    assert forward["deliverable"] is True

    # An edit to the target page moves its signature but not the proposal's
    # evidence: same fingerprint, and the settle clock starts again.
    fx.edit(
        vault,
        fx.INLET,
        fx.insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-04",
            observation="Inlet pressure falls well before cavitation begins.",
        ),
    )
    _quiet(vault, now=LATER + 60)
    touched = _pair(_rows(vault), fx.CAVITATION, fx.INLET)
    assert touched["fingerprint"] == forward["fingerprint"]
    assert touched["deliverable"] is False

    # Moving both pages to another shared source changes the evidence itself.
    for rel, title, observation in (
        (fx.CAVITATION, "Pump cavitation", "Cavitation starts above 40 litres a minute."),
        (fx.INLET, "Pump inlet pressure", "Inlet pressure falls well before cavitation begins."),
    ):
        fx.edit(
            vault,
            rel,
            fx.insight(title, sources=["field-report-three"], updated="2026-05-05",
                       observation=observation),
            graph=False,
        )
    fx.publish_graph(vault)
    _quiet(vault, now=LATER + 120)
    moved = _pair(_rows(vault), fx.CAVITATION, fx.INLET)
    assert moved["id"] == forward["id"]
    assert moved["fingerprint"] != forward["fingerprint"]
    assert {item["path"] for item in moved["evidence"]} == {
        fx.CAVITATION,
        fx.INLET,
        fx.SOURCE_THREE,
    }


def test_superseded_endpoint_yields_nothing(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _quiet(vault)
    assert _pair(_rows(vault), fx.CAVITATION, fx.INLET) is not None
    fx.edit(
        vault,
        fx.INLET,
        fx.insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-03",
            status="superseded",
            observation="Inlet pressure falls before cavitation begins.",
        ),
    )
    _quiet(vault)
    open_rows = _rows(vault)
    assert _pair(open_rows, fx.CAVITATION, fx.INLET) is None
    assert _pair(open_rows, fx.INLET, fx.CAVITATION) is None


def test_a_symmetric_pair_is_delivered_once(tmp_path: Path) -> None:
    vault = fx.build(tmp_path)
    _settled(vault)
    rows = _rows(vault)
    forward = _pair(rows, fx.CAVITATION, fx.INLET)
    reverse = _pair(rows, fx.INLET, fx.CAVITATION)
    assert forward is not None and reverse is not None
    assert forward["deliverable"] is True
    assert reverse["deliverable"] is False
    # Dismissing the offered direction lets the other one stand alone.
    relation_queue.triage(
        vault, ref=forward["ref"], action="dismiss", why="handled: x", source_path=fx.CAVITATION
    )
    _quiet(vault, now=LATER + 60)
    rows = _rows(vault)
    assert _pair(rows, fx.CAVITATION, fx.INLET)["deliverable"] is False
    assert _pair(rows, fx.INLET, fx.CAVITATION)["deliverable"] is True
