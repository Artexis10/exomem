"""`recap.covered_by`: a recap's Decided lines read against the page they name.

Most tests judge the queued claims with a stub instrument and append the
readings themselves (`sense_coverage`), which lets them replay in any order.
The worker tests at the end run the sensor child's own loop.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf

from exomem import add as add_module
from exomem import (
    dreamer,
    freshness,
    sensed_model,
    sensing,
    sensing_ledger,
    sensor_worker,
    source_taxonomy,
)
from exomem import episode_capture as capture

WHEN = dt.datetime(2026, 5, 18, 9, 12, 33, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 5, 19, 9, 12, 33, tzinfo=dt.UTC)
KEY = "ep-" + "a1" * 16
ELSEWHERE = "ep-" + "b2" * 16
LINK = "[[Notes/Insights/seal-wear]]"
LINE = f"Seal wear doubles after a dry start, most of all in winter ({LINK})"
CLAIM = "Seal wear doubles after a dry start, most of all in winter (seal-wear)"
OTHER_LINE = f"Seal wear never changes after a dry start ({LINK})"
OTHER_CLAIM = "Seal wear never changes after a dry start (seal-wear)"
COVERAGE = (sensing.RECAP_COVERED_BY,)


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


def prepared(decided, *, key=KEY, when=WHEN) -> capture.Recap:
    return capture.prepare(
        episode=key, subject="Seal session", summary="Seal wear session.",
        worked_on=["Reviewed seal wear"], decided=list(decided), open=None, said=None,
        about=None, client=None, when=when, audience="owner",
    )


def recap_page(decided, *, key=KEY, when=WHEN, status=None) -> tuple[str, str]:
    """`(path, text)` of a recap page laid out the way the Source writer lays it out."""
    recap = prepared(decided, key=key, when=when)
    path = f"{fx.EPISODES}/{when:%Y-%m-%d}-{recap.slug_at(capture.order_token(when))}.md"
    text = (
        f"---\ntitle: Seal session\ntype: source\nsource_type: episode\n"
        f"captured: {when:%Y-%m-%d}\nsummary: {recap.summary}\nepisode: {key}\n"
        f"episode_digest: {recap.digest}\n" + (f"status: {status}\n" if status else "")
        + f"ingested_into: []\n---\n\n# Seal session\n\n## Capture\n\n{recap.body}\n"
    )
    return path, text


def start(tmp_path: Path, monkeypatch, *pages: tuple[str, str]) -> Path:
    """The seal-wear vault with coverage sensing on and these recap pages added."""
    sf.enable(monkeypatch, sf.STUB, sf.COVERAGE_STUB)
    vault = sf.build(tmp_path)
    for path, text in pages:
        fx.edit(vault, path, text)
    sf.settle(vault)
    return vault


def seal_page(*units: str) -> str:
    """The home page with these further units after its first one."""
    return sf.note("Seal wear", "2026-04-01", sf.SEAL_TEXT,
                   extra="".join(f"- [finding] {unit}\n" for unit in units))


def sense_coverage(vault: Path, instrument, *, reverse: bool = False, at: str = "2026-05-20T00:00:00Z") -> int:
    """Judge every queued claim and append the readings, as the worker's loop will."""
    conn = sensed_model.open_readonly(vault)
    try:
        queued = sensed_model.queued_pairs(conn, 256, question_types=COVERAGE)
    finally:
        conn.close()
    results = instrument.judge([pair.texts for pair in queued])
    readings = [
        sensing.make_reading(
            pair.question_type, instrument.identity, list(pair.inputs), ab=ab, ba=ba,
            abstain_reason=reason, sensed_at=at,
        )
        for pair, (ab, ba, reason) in zip(queued, results, strict=True)
    ]
    ledger = sensing_ledger.Ledger(vault)
    lconn = ledger.connect()
    ledger.append(lconn, list(reversed(readings)) if reverse else readings)
    lconn.close()
    return len(readings)


def coverage_rows(vault: Path) -> list[tuple]:
    conn = sensed_model.open_readonly(vault)
    try:
        return conn.execute(
            "SELECT pair_key, path_a, unit_a, hash_a, path_b, unit_b, hash_b, state, verdict, "
            "reading_id, fingerprint, selected, queued FROM coverage_pairs ORDER BY pair_key"
        ).fetchall()
    finally:
        conn.close()


def states(vault: Path) -> dict[str, str]:
    """Each claim's state by its text."""
    return {
        claim.text: claim.state
        for item in sensed_model.coverage_items(vault)
        for claim in item.claims
    }


def due(vault: Path) -> list[sensed_model.CoverageItem]:
    return [item for item in sensed_model.coverage_items(vault) if item.due]


def test_a_linked_decided_line_is_a_claim_that_names_its_home(
    vault: Path, source_schema
) -> None:
    """The page the Source writer really produces yields display text without link syntax."""
    recap = prepared([LINE, "Order new seals on Monday", f"Retire the old rig ({LINK[:-2]}|the seal note]])"])
    result = add_module.add(
        vault, source_schema, content=recap.body, title=recap.subject,
        source_type=source_taxonomy.EPISODE_KIND, slug=recap.slug_at(capture.order_token(WHEN)),
        today=WHEN, extra_frontmatter=recap.frontmatter,
    )
    raw = (vault / result.path).read_text(encoding="utf-8")

    claims = sensed_model.recap_claims(raw, [sf.SEAL])

    assert [(claim.text, claim.home) for claim in claims] == [
        (CLAIM, sf.SEAL),
        ("Retire the old rig (the seal note)", sf.SEAL),
    ], "an unlinked line is not a claim, and an alias is what a reader sees"
    assert claims[0].text_sha256 == sensing.text_sha256(CLAIM)


def test_a_link_that_names_two_pages_is_no_claim() -> None:
    """A guessed home would send a compile to the wrong page."""
    _path, raw = recap_page([LINE])
    duplicate = f"{fx.KB}/Archive/Insights/seal-wear.md"

    assert sensed_model.recap_claims(raw, [sf.SEAL, duplicate]) != []
    assert sensed_model.recap_claims(raw.replace(LINK, "[[seal-wear]]"), [sf.SEAL, duplicate]) == []


def test_an_edited_recap_yields_no_claims() -> None:
    """Free prose that borrows the headings is not something the recorder wrote."""
    _path, raw = recap_page([LINE])

    assert sensed_model.recap_claims(raw, [sf.SEAL]) != []
    assert sensed_model.recap_claims(raw.replace("\n- ", "\n-  ", 1), [sf.SEAL]) == []


def test_an_absent_claim_queues_and_projects_absent(tmp_path: Path, monkeypatch) -> None:
    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    relation = sensed_model.queue_depth(vault)
    assert sensed_model.queue_depth(vault, question_types=COVERAGE) == 1
    assert sensed_model.queue_depth(
        vault, question_types=(sensing.PAIR_RELATION, sensing.RECAP_COVERED_BY)
    ) == relation + 1, "the worker's default queue never counts coverage"
    conn = sensed_model.open_readonly(vault)
    default = sensed_model.queued_pairs(conn, 256)
    [pair] = sensed_model.queued_pairs(conn, 256, question_types=COVERAGE)
    conn.close()
    assert {item.question_type for item in default} == {sensing.PAIR_RELATION}
    assert [item.slot for item in pair.inputs] == ["premise", "hypothesis"]
    assert pair.texts == (sf.SEAL_TEXT, CLAIM)
    assert states(vault) == {CLAIM: "incomplete"}

    assert sense_coverage(vault, sf.CoverageStubInstrument()) == 1
    sf.settle(vault)

    assert states(vault) == {CLAIM: "absent"}
    [item] = due(vault)
    assert item.home == sf.SEAL and [claim.text for claim in item.due] == [CLAIM]
    assert sensed_model.queue_depth(vault, question_types=COVERAGE) == 0


def test_compiling_the_claim_into_its_home_clears_it_as_entailed(tmp_path: Path, monkeypatch) -> None:
    """The product loop: the home stays due until a re-sense reads the claim entailed."""
    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    stub = sf.CoverageStubInstrument()
    sense_coverage(vault, stub)
    sf.settle(vault)
    assert states(vault) == {CLAIM: "absent"}

    fx.edit(vault, sf.SEAL, seal_page(sf.WINTER_TEXT))
    sf.settle(vault)

    assert states(vault) == {CLAIM: "incomplete"}, "the new unit is unread: nothing clears yet"
    assert due(vault) == [], "and an unread unit must not prompt a second compile"

    stub.table[(sf.WINTER_TEXT, CLAIM)] = "entailed"
    assert sense_coverage(vault, stub) == 1, "only the new unit is read again"
    sf.settle(vault)

    assert states(vault) == {CLAIM: "entailed"}
    assert due(vault) == []


def test_a_second_recap_on_the_home_never_moves_a_coverage_selection(
    tmp_path: Path, monkeypatch
) -> None:
    """The cap is per (home, recap): another recap of the same home moves none of this one's pairs."""
    monkeypatch.setattr(sensed_model, "PAIR_CAP", 3)
    vault = start(tmp_path, monkeypatch, recap_page([LINE, OTHER_LINE]))
    fx.edit(vault, sf.SEAL, seal_page(*(f"Seal fact {i} holds." for i in range(1, 4))))
    sf.settle(vault)
    first = recap_page([LINE, OTHER_LINE])[0]
    before = coverage_rows(vault)
    assert len(before) == 8 and sum(row[11] for row in before) == 3

    fx.edit(vault, *recap_page([f"Seal wear is logged weekly ({LINK})"], key=ELSEWHERE, when=LATER))
    sf.settle(vault)

    assert [row for row in coverage_rows(vault) if row[4] == first] == before


def test_a_capped_home_reads_incomplete(tmp_path: Path, monkeypatch) -> None:
    """A claim whose home has more units than the cap could still be entailed by an unread one."""
    monkeypatch.setattr(sensed_model, "PAIR_CAP", 2)
    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    fx.edit(vault, sf.SEAL, seal_page(*(f"Seal fact {i} holds." for i in range(1, 4))))
    sf.settle(vault)

    assert sense_coverage(vault, sf.CoverageStubInstrument()) == 2
    sf.settle(vault)

    assert states(vault) == {CLAIM: "incomplete"}
    assert due(vault) == []


def test_a_retired_recap_revision_proposes_nothing(tmp_path: Path, monkeypatch) -> None:
    """Only the live revision speaks: a superseded one would prompt compiles of a decision since changed."""
    old = recap_page([LINE], status="superseded")
    live = recap_page([OTHER_LINE], when=LATER)
    vault = start(tmp_path, monkeypatch, old, live)

    conn = sensed_model.open_readonly(vault)
    episodes = {row[0] for row in conn.execute("SELECT episode_path FROM claims")}
    conn.close()

    assert episodes == {live[0]}
    assert states(vault) == {OTHER_CLAIM: "incomplete"}


def test_a_replay_in_reverse_order_is_byte_identical(tmp_path: Path, monkeypatch) -> None:
    vault = start(tmp_path, monkeypatch, recap_page([LINE, OTHER_LINE]))
    stub = sf.CoverageStubInstrument({(sf.SEAL_TEXT, CLAIM): "partial"})
    sense_coverage(vault, stub)
    sf.settle(vault)
    before = coverage_rows(vault)
    assert {row[8] for row in before} == {"partial", "absent"}

    ledger = sensing_ledger.ledger_path(vault)
    for suffix in ("", "-wal", "-shm"):
        ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)
    sensed_model.ProjectionStore(vault).wipe()
    sf.settle(vault)
    assert {row[7] for row in coverage_rows(vault)} == {"pending"}
    sense_coverage(vault, stub, reverse=True, at="1999-01-01T00:00:00Z")
    sf.settle(vault)

    assert coverage_rows(vault) == before


def test_the_pair_relation_projection_is_unchanged_with_coverage_active(
    tmp_path: Path, monkeypatch
) -> None:
    """Adding a question must not re-key, re-read or re-select another question's edges."""
    page = recap_page([LINE])

    def converged(root: Path, *identities) -> tuple[list[tuple], object]:
        sf.enable(monkeypatch, *identities)
        vault = sf.build(root)
        fx.edit(vault, *page)
        sf.converge(vault, sf.StubInstrument(sf.default_table()))
        conn = sensed_model.open_readonly(vault)
        try:
            return sf.edges(vault), sensed_model._get_meta(conn, "active_key")
        finally:
            conn.close()

    alone, alone_key = converged(tmp_path / "alone", sf.STUB)
    both, both_key = converged(tmp_path / "both", sf.STUB, sf.COVERAGE_STUB)

    assert alone and both == alone
    assert both_key == alone_key == sensed_model._active_key({sf.STUB.instrument_id: sf.STUB}, "relation-v1")


def run_child(vault: Path, **kwargs) -> int:
    return sensor_worker.run_child(
        vault, parent_pid=0, cpu_allotment=1e9, judgement_allotment=10**6, idle_seconds=0.0,
        sleep=lambda _seconds: None, **kwargs,
    )


def ledger_counts(vault: Path) -> dict[str, int]:
    conn = sensing_ledger.open_readonly(vault)
    try:
        return dict(conn.execute("SELECT question_type, count(*) FROM readings GROUP BY 1"))
    finally:
        conn.close()


def test_one_child_senses_both_questions_from_one_queue(tmp_path: Path, monkeypatch) -> None:
    """A worker that sensed only pair.relation would leave every claim `incomplete` for ever."""
    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    relation = sf.StubInstrument(sf.default_table())
    coverage = sf.CoverageStubInstrument({(sf.SEAL_TEXT, CLAIM): "partial"})

    assert run_child(vault, instrument=[relation, coverage]) == sensor_worker.EXIT_IDLE
    sf.settle(vault)

    assert coverage.calls == [(sf.SEAL_TEXT, CLAIM)], "the claim is read in slot order"
    assert relation.calls and (sf.SEAL_TEXT, CLAIM) not in relation.calls
    assert set(ledger_counts(vault)) == {sensing.PAIR_RELATION, sensing.RECAP_COVERED_BY}
    assert states(vault) == {CLAIM: "partial"}
    assert {row[5] for row in sf.edges(vault)} >= {"current"}, "the pair edges are served as before"


def test_a_refused_question_does_not_relaunch_the_child_for_its_own_queue(
    tmp_path: Path, monkeypatch
) -> None:
    """Each relaunch loads the model for nothing, and the hourly budget pays for the loop."""
    from exomem import sensing_nli

    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    relation = sf.StubInstrument(sf.default_table())

    def admit(question_type: str):
        if question_type == sensing.RECAP_COVERED_BY:
            raise sensing_nli.Refused("fixtures-failed", "a coverage fixture read wrongly")
        return relation

    class Ended:
        """A finished child: the sensor loop ran in this process."""

        pid = os.getpid()
        returncode = sensor_worker.EXIT_IDLE

        def poll(self):
            return self.returncode

    launches: list[int] = []

    def launch(_vault, _cpu, _judgements):
        launches.append(run_child(vault, admit=admit))
        return Ended()

    supervisor = sensor_worker.Supervisor(vault, launch=launch)

    def step() -> str:
        return supervisor.step(
            gate_run=True, gate_reason="ready",
            queue_depth=lambda questions: sensed_model.queue_depth(vault, question_types=questions),
        )

    assert step() == "launched"
    sf.settle(vault)
    assert sensed_model.queue_depth(vault, question_types=COVERAGE) == 1, "the claim is still queued"

    assert step() == "idle", "the coverage queue is not counted for a refused question"
    assert launches == [sensor_worker.EXIT_IDLE] and supervisor.launches == 1
    assert supervisor.status()["refused"] == {sensing.RECAP_COVERED_BY: "fixtures-failed"}
    assert set(ledger_counts(vault)) == {sensing.PAIR_RELATION}, "the other question still ran"
    assert states(vault) == {CLAIM: "incomplete"}
