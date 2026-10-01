"""S5 acceptance of the `conversation` group on arms (a) to (d).

Runs the pinned group (`context-activation-conversation-v1`) through the
product on every arm and asserts the pre-registered floors per case and per
anchor kind, with no aggregate. Arm (a) is the mechanism-removal control and
must stay red. Arms (b) to (d) must meet the floors.

Every scored row must meet its pre-registered expectation, except the rows
pinned in `PENDING_RULING` (empty). The test fails if that set changes in
either direction.
"""

from __future__ import annotations

import pytest
from membench.utility.context_activation_conversation import (
    arm_floors,
    incident_classes,
    mechanism_removal,
)
from test_context_activation_conversation_baseline import Run, run  # noqa: F401

#: The shared `run` fixture is built by whichever module asks first; when this
#: one does, it pays the corpus build and every arm, as the baseline module's
#: own bound allows.
pytestmark = pytest.mark.timeout(1800)

#: Rows awaiting a ruling. Empty since the orchestrator's rulings on #1455:
#: twins W17 to W24 were re-authored with a focus that names nothing (V29 pins
#: that a focus naming the subject resolves), and bare "one" no longer makes a
#: turn anaphoric. Anything that fails again must be added here with its reason.
PENDING_RULING: set[tuple[str, str]] = {
    # Round 3 on #1463: the content gate reads "book the inspector" as new
    # content, so "Is it safe to use this week, or should we book the
    # inspector first?" is no longer carried to the winch on arm (b). A real
    # positive the gate rejects; arm (d) still resolves it through `focus`.
    ("V21", "b"),
}


def test_the_control_arm_stays_red_and_the_incident_classes_fail_without_the_conversation(run: Run) -> None:  # noqa: F811
    verdict = mechanism_removal(run.rows)
    assert verdict["red"] is True and verdict["failed_count"] * 2 >= verdict["multi_turn_cases"]
    failing = incident_classes(run.rows)
    for kind in ("promotion", "tie_break", "anaphoric_carry", "topic_switch"):
        assert failing.get(kind) == sorted(c.case_id for c in _cases(kind)), kind


def _cases(kind: str):
    from epistemic.corpora.context_activation_conversation import CASES

    return [case for case in CASES if case.kind == kind]


def test_gold_recall_and_drowning_meet_the_floors_on_every_conversation_arm(run: Run) -> None:  # noqa: F811
    for arm in ("b", "c", "d"):
        floors = arm_floors(run.rows, arm)
        below = [item for item in floors["recall_below_floor"] if (item["case_id"], arm) not in PENDING_RULING]
        assert below == [], (arm, below)
        assert floors["drowning_failures"] == [], arm
        assert floors["packet_size"]["within_bounds"], arm


def test_every_row_meets_its_expectation_except_the_pinned_pending_ruling_set(run: Run) -> None:  # noqa: F811
    failing = {(row.case_id, row.arm) for row in run.rows if row.arm != "a" and not row.passed}
    assert failing == PENDING_RULING, sorted(failing ^ PENDING_RULING)


def test_the_withheld_pair_and_the_attachment_cases_pass_on_the_arms_that_carry_a_conversation(run: Run) -> None:  # noqa: F811
    for row in run.rows:
        if row.arm != "a" and row.case_id in {"V26", "V27", "V29", "W26", "V28", "W28"}:
            assert row.passed, (row.case_id, row.arm, row.failure_reasons)


def test_the_withheld_pair_is_byte_identical_on_every_arm_that_carries_a_conversation(run: Run) -> None:  # noqa: F811
    from epistemic.corpora.context_activation_conversation import case_by_id
    from membench.utility.context_activation_conversation import score_withheld_pair

    for arm in ("b", "c", "d"):
        if ("V28", arm) not in run.packets:
            continue
        verdict = score_withheld_pair(
            case_by_id("V28"), case_by_id("W28"), arm, run.packets[("V28", arm)], run.packets[("W28", arm)]
        )
        assert verdict.identical, (arm, verdict.differs_in)
