"""The effect of `upkeep_sensed_compile`: what became of the items, and what recaps said overall.

`acted` is the one column a family can fill only by telling a changed home from a
re-read one, so each case here changes the world a different way and reads the
dispositions view the owner reads. Every other family reports `acted: unknown`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import dreamer_fixture as fx
import pytest
import sensing_fixture as sf
from test_sensed_compile_delivery import FAMILY, two_recaps
from test_sensed_coverage import (
    CLAIM,
    LINE,
    LINK,
    OTHER_CLAIM,
    OTHER_LINE,
    WHEN,
    recap_page,
    seal_page,
    sense_coverage,
    start,
)
from test_upkeep_vocabulary_egress_twin import _reset

from exomem import commands, freshness, review_state, sensed_model, sensing, sensor_worker, upkeep
from exomem.command_surface import resolve_invocation
from exomem.writer_lease import invoke_command


@pytest.fixture(autouse=True)
def _clean():
    _reset()
    sensor_worker.reset_for_tests()
    sensed_model._VECTORS.clear()
    yield
    _reset()
    sensor_worker.reset_for_tests()
    freshness.clear()
    sensed_model._VECTORS.clear()


def effect(vault: Path, **kwargs) -> dict:
    return commands.op_review_memory(vault, mode="dispositions", **kwargs)["effect"]


def family_row(vault: Path) -> dict:
    return effect(vault)["families"][FAMILY]


def surfaced_once(tmp_path: Path, monkeypatch) -> tuple[Path, sf.CoverageStubInstrument]:
    """One recap claim the home does not state, served once by the review list."""
    stub = sf.CoverageStubInstrument()
    vault = start(tmp_path, monkeypatch, recap_page([LINE]))
    sense_coverage(vault, stub)
    sf.settle(vault)
    assert [i["family"] for i in upkeep.review(vault, limit=50)["items"] if i["family"] == FAMILY]
    return vault, stub


def counts(row: dict) -> dict:
    return {key: row[key] for key in ("surfaced", "dismissed", "snoozed", "cleared", "acted", "open")}


def test_a_home_that_now_states_the_claim_counts_as_acted(tmp_path: Path, monkeypatch) -> None:
    vault, stub = surfaced_once(tmp_path, monkeypatch)
    assert counts(family_row(vault)) == {
        "surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 0, "acted": 0, "open": 1,
    }

    fx.edit(vault, sf.SEAL, seal_page(sf.WINTER_TEXT))
    sf.settle(vault)
    assert counts(family_row(vault))["acted"] == 0, "the new unit is unread: no change is proven yet"
    assert counts(family_row(vault))["cleared"] == 1

    stub.table[(sf.WINTER_TEXT, CLAIM)] = "entailed"
    sense_coverage(vault, stub)
    sf.settle(vault)

    assert counts(family_row(vault)) == {
        "surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 0, "acted": 1, "open": 0,
    }


def test_a_deleted_home_counts_as_cleared(tmp_path: Path, monkeypatch) -> None:
    vault, _stub = surfaced_once(tmp_path, monkeypatch)

    fx.remove(vault, sf.SEAL)
    sf.settle(vault)

    assert counts(family_row(vault)) == {
        "surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 1, "acted": 0, "open": 0,
    }


def test_a_label_map_change_counts_as_neither(tmp_path: Path, monkeypatch) -> None:
    """A looser map reads the same unit as entailing: the page did not change, so nobody acted."""
    vault, _stub = surfaced_once(tmp_path, monkeypatch)
    question = sensing.QUESTIONS[sensing.RECAP_COVERED_BY]
    loose = sensing.CoverageLabelMap(
        version="coverage-v2", columns=("entailment", "neutral", "contradiction"),
        entailed_min=0.1, partial_min=0.1,
    )
    monkeypatch.setitem(sensing.LABEL_MAPS, "coverage-v2", loose)
    monkeypatch.setitem(
        sensing.QUESTIONS,
        sensing.RECAP_COVERED_BY,
        sensing.Question(
            question.question_type, question.template_version, "coverage-v2",
            question.unit_scope, question.fixture_set,
        ),
    )

    sf.settle(vault)

    assert not sensed_model.compile_rows(vault).rows, "the claim now reads entailed by the same unit"
    assert counts(family_row(vault)) == {
        "surfaced": 1, "dismissed": 0, "snoozed": 0, "cleared": 1, "acted": 0, "open": 0,
    }


def test_every_other_family_reports_acted_as_unknown(tmp_path: Path, monkeypatch) -> None:
    """A family that cannot tell a change from a re-read must say so, never report 0."""
    vault, _stub = two_recaps(tmp_path, monkeypatch)
    upkeep.review(vault, limit=50)
    review_state.record_surfaced(vault, [("1" * 24, "e" * 24, "relation_debt")], surface="review")

    block = effect(vault)

    assert block["families"][FAMILY]["acted"] == 0
    assert block["families"]["relation_debt"]["acted"] == "unknown"
    assert block["sources"]["surfaced"].startswith("a lower bound")
    assert "unknown for every other family" in block["sources"]["acted"]


def test_the_window_bounds_are_stated_and_a_value_out_of_range_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    vault, _stub = surfaced_once(tmp_path, monkeypatch)

    command, flat = resolve_invocation(
        "read_review",
        {"operation": {"kind": "dispositions", "window_days": 30}},
        commands.PRODUCT_COMMANDS,
    )
    month = invoke_command(command, vault, **flat)["effect"]["window"]
    through = dt.date.fromisoformat(month["through"])
    assert month["days"] == 30 and dt.date.fromisoformat(month["since"]) == through - dt.timedelta(days=29)
    assert effect(vault)["window"]["days"] == 7, "the default stays a week"
    assert effect(vault, window_days=1)["window"]["since"] == effect(vault, window_days=1)["window"]["through"]

    for bad in (0, 91, -1, True):
        with pytest.raises(ValueError, match=r"INVALID_WINDOW_DAYS: .*from 1 to 90"):
            effect(vault, window_days=bad)
    with pytest.raises(ValueError, match="INVALID_REVIEW_ARGUMENTS: window_days is only supported"):
        commands.op_review_memory(vault, mode="upkeep", window_days=7)


def test_the_coverage_line_counts_each_claim_by_what_its_home_says(tmp_path: Path, monkeypatch) -> None:
    now = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    stub = sf.CoverageStubInstrument(
        {(sf.WINTER_TEXT, CLAIM): "entailed", (sf.SEAL_TEXT, OTHER_CLAIM): "partial"}
    )
    vault = start(
        tmp_path, monkeypatch,
        recap_page([LINE, OTHER_LINE, "Order new seals on Monday"], when=now),
    )
    fx.edit(vault, sf.SEAL, seal_page(sf.WINTER_TEXT))
    sf.settle(vault)
    assert effect(vault)["coverage"][FAMILY]["claims"]["incomplete"] == 2, "unread is not absent"

    sense_coverage(vault, stub)
    sf.settle(vault)
    line = effect(vault)["coverage"][FAMILY]

    assert line == {
        "claims": {"entailed": 1, "partial": 1, "absent": 0, "abstain": 0, "incomplete": 0},
        "unlinked_decided": 1,
        "evidence_complete": True,
    }

    fx.edit(vault, *recap_page([f"Seal wear is logged weekly ({LINK})"], key="ep-" + "c3" * 16, when=now))
    sf.settle(vault)
    later = effect(vault)["coverage"][FAMILY]
    assert later["claims"]["incomplete"] == 1 and later["evidence_complete"] is False
    assert effect(vault, window_days=2)["coverage"][FAMILY]["claims"]["entailed"] == 1


def test_a_recap_outside_the_window_is_not_counted(tmp_path: Path, monkeypatch) -> None:
    vault = start(tmp_path, monkeypatch, recap_page([LINE], when=WHEN))
    sense_coverage(vault, sf.CoverageStubInstrument())
    sf.settle(vault)

    empty = {"entailed": 0, "partial": 0, "absent": 0, "abstain": 0, "incomplete": 0}
    assert effect(vault, window_days=7)["coverage"][FAMILY]["claims"] == empty
    assert effect(vault, window_days=90)["coverage"][FAMILY]["claims"] == empty, "a May recap is past 90 days"


def test_a_projection_that_cannot_say_leaves_the_numbers_unknown(tmp_path: Path, monkeypatch) -> None:
    sf.enable(monkeypatch, sf.STUB, sf.COVERAGE_STUB)
    vault = sf.build(tmp_path)

    block = effect(vault)

    assert block["coverage"][FAMILY] == {
        "claims": "unknown", "unlinked_decided": "unknown",
        "evidence_complete": False, "unknown_reason": "projection_unavailable",
    }
    row = block["families"][FAMILY]
    assert (row["cleared"], row["acted"], row["open"]) == ("unknown", "unknown", "unknown")


def test_sensing_off_adds_no_coverage_line(tmp_path: Path) -> None:
    vault = sf.build(tmp_path)

    block = effect(vault)

    assert "coverage" not in block and "coverage" not in block["sources"]
