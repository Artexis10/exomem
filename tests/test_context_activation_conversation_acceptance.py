"""S5 acceptance of the `conversation` group on arms (a) to (d).

Runs the pinned group (`context-activation-conversation-v1`) through the
product on every arm and asserts the pre-registered floors per case and per
anchor kind, with no aggregate. Arm (a) is the mechanism-removal control and
must stay red. Arms (b) to (d) must meet the floors.

Nine (case, arm) rows do not meet them, and the fixtures may not be edited
after a manifest references their digest, so they are pinned here as the exact
set awaiting an orchestrator ruling (see the PR, "Needs ruling"). The test
fails if that set changes in either direction: a fix must delete rows from
`PENDING_RULING`, never silently pass.
"""

from __future__ import annotations

from membench.utility.context_activation_conversation import (
    arm_floors,
    incident_classes,
    mechanism_removal,
)
from test_context_activation_conversation_baseline import Run, run  # noqa: F401

#: Twins whose CONTENT-FREE current turn carries a fixture-authored `focus`
#: naming the earlier subject. `focus` is current-turn evidence by ruling, so an
#: exact alias in it resolves that anchor (the attachment cases depend on this):
#: the pre-registered "twin resolves nothing" expectation contradicts the ruled
#: semantics on arms (c) and (d).
FOCUS_NAMES_THE_POISON = {(f"W{n}", arm) for n in range(17, 25) for arm in ("c", "d")}
#: "Cheers, there is plenty of chat for one day." speaks the shipped follow-up
#: marker "one", so it is anaphoric and the conversation carry serves the
#: earlier subject as a `partial` anchor (arm b: no `focus`).
BARE_ONE_IS_ANAPHORIC = {("W24", "b")}
PENDING_RULING = FOCUS_NAMES_THE_POISON | BARE_ONE_IS_ANAPHORIC


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
        assert floors["recall_below_floor"] == [], (arm, floors["recall_below_floor"])
        assert floors["drowning_failures"] == [], arm
        assert floors["packet_size"]["within_bounds"], arm


def test_every_row_meets_its_expectation_except_the_pinned_pending_ruling_set(run: Run) -> None:  # noqa: F811
    failing = {(row.case_id, row.arm) for row in run.rows if row.arm != "a" and not row.passed}
    assert failing == PENDING_RULING, sorted(failing ^ PENDING_RULING)


def test_the_withheld_pair_and_the_attachment_cases_pass_on_the_arms_that_carry_a_conversation(run: Run) -> None:  # noqa: F811
    for row in run.rows:
        if row.arm != "a" and row.case_id in {"V26", "V27", "W26", "V28", "W28"}:
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
