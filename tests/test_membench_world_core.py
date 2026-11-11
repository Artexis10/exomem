"""The world core and its validator: one world built from operations validates clean, and each
invariant has one small world that breaks it, so the validator must name the invariant and the item.

``sound_world`` builds the clean starting point. A failing case changes one thing in a copy of it,
through the same operations a recipe uses where an operation can express the fault.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest
from membench.clock import week_date

from benchmarks.membench.world.core import StepRefused, Vocabulary, World, implemented
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.validator import WorldInvalid, check, validate

VOCABULARY = Vocabulary.load()
WORLD_DIR = Path(__file__).resolve().parent.parent / "benchmarks" / "membench" / "world"


def sound_world(name: str = "w") -> World:
    """Two holders, an office with a durable and a permanent attribute, and three events."""
    world = World(name, VOCABULARY)
    world.add_entity("lead", "Mara", week_date(0), holder=True)
    world.add_entity("clerk", "Odo", week_date(0), holder=True)
    world.add_entity("office", "Harbour office", week_date(0))
    world.add_entity("depot", "North depot", week_date(0))
    world.add_attribute("approvals", "office", "durable")
    world.add_attribute("founded", "office", "permanent")
    for event in ("outage", "restart", "audit"):
        world.add_event(event)
    world.add_context("everywhere")
    world.add_context("quay", parent="everywhere")
    world.add_audience("insiders", ["lead"])
    return world


def faults(world: World) -> set[tuple[str, str]]:
    return {(r.code, r.item) for r in validate(world)}


def test_a_world_built_from_every_declared_operation_validates_clean() -> None:
    steps = [
        {"op": "set", "target": "approvals", "value": 1, "at": week_date(1)},
        {"op": "state", "id": "first", "by": "lead", "about": "approvals", "value": 1,
         "kind": "decision", "at": week_date(1)},
        {"op": "set", "target": "approvals", "value": 2, "at": week_date(3), "world": "planted"},
        {"op": "state", "id": "second", "by": "lead", "about": "approvals", "value": 2,
         "kind": "decision", "at": week_date(3), "world": "planted"},
        {"op": "state", "id": "mistaken", "by": "clerk", "about": "approvals", "value": 9,
         "kind": "fact", "basis": "told", "quoting": "lead", "hedge": "tentative",
         "scope": "quay", "as": "Harbour office", "at": week_date(3)},
        {"op": "correct", "by": "lead", "statement": "mistaken", "value": 2, "at": week_date(4)},
        {"op": "withdraw", "by": "clerk", "statement": "mistaken", "at": week_date(5)},
        {"op": "support", "from": "first", "to": "second"},
        {"op": "defeat", "from": "second", "to": "mistaken", "mode": "rebut"},
        {"op": "rule", "id": "odds", "by": "lead", "shape": "chance", "over": "outage",
         "value": 0.3, "at": week_date(1)},
        {"op": "set", "target": "outage", "at": week_date(2)},
        {"op": "set", "target": "restart", "at": week_date(2, 3)},
        {"op": "draw", "target": "outage", "chance": "odds", "at": week_date(2)},
        {"op": "cause", "from": "outage", "to": "restart"},
        {"op": "name", "entity": "office", "name": "Quay office", "replaces": "Harbour office",
         "at": week_date(4)},
        {"op": "merge", "entities": ["office", "depot"], "at": week_date(6)},
        {"op": "tell", "holder": "clerk", "statement": "first", "at": week_date(2)},
        {"op": "restrict", "target": "first", "audience": "insiders", "by": "lead",
         "at": week_date(2), "until": week_date(8)},
        {"op": "end", "target": "approvals", "at": week_date(9)},
    ]  # fmt: skip
    world = sound_world()
    made = world.apply_all(steps, in_world="planted")
    world.add_artefact("note", week_date(1), carries=["first"], by="lead")
    world.apply({"op": "copy", "artefact": "first", "at": week_date(5)})
    world.apply({"op": "remove", "artefact": "note", "at": week_date(7)})

    assert validate(world) == []
    assert "second" in made and "mistaken" in made
    assert world.value_at("approvals", week_date(4)) == 2
    assert world.value_at("approvals", week_date(10)) is None


def test_the_mutation_world_leaves_out_the_planted_steps() -> None:
    steps = [
        {"op": "set", "target": "approvals", "value": 1, "at": week_date(1)},
        {"op": "set", "target": "approvals", "value": 2, "at": week_date(3), "world": "planted"},
        {"op": "set", "target": "approvals", "value": 3, "at": week_date(3), "world": "mutation"},
    ]
    planted, mutation = sound_world(), sound_world()
    planted.apply_all(steps, in_world="planted")
    mutation.apply_all(steps, in_world="mutation")

    assert planted.value_at("approvals", week_date(4)) == 2
    assert mutation.value_at("approvals", week_date(4)) == 3


def test_the_core_implements_every_operation_that_the_catalogue_declares() -> None:
    assert set(implemented()["world_operations"]) == set(VOCABULARY.operations)
    assert load_catalogue(implemented=implemented()).buildable == load_catalogue().buildable


def test_an_undeclared_operation_is_refused_by_name() -> None:
    with pytest.raises(StepRefused, match="undeclared-operation: omit"):
        sound_world().apply({"op": "omit", "target": "approvals"})


def test_an_undeclared_argument_is_refused_by_name() -> None:
    with pytest.raises(StepRefused, match="undeclared-argument: set: `colour`"):
        sound_world().apply(
            {"op": "set", "target": "approvals", "at": week_date(1), "colour": "red"}
        )


def test_a_missing_argument_is_refused_by_name() -> None:
    with pytest.raises(StepRefused, match="missing-argument: state: `by`"):
        sound_world().apply({"op": "state", "about": "approvals", "at": week_date(1)})


def test_an_id_used_twice_is_refused_by_name() -> None:
    world = sound_world()
    step = {"op": "state", "id": "a", "by": "lead", "about": "approvals", "kind": "fact",
            "at": week_date(1)}  # fmt: skip
    world.apply(step)
    with pytest.raises(StepRefused, match="duplicate-id: a"):
        world.apply(step)


def test_check_raises_every_fault_together() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "approvals", "value": 1, "at": week_date(2)})
    world.apply({"op": "set", "target": "approvals", "value": 2, "at": week_date(2)})
    world.apply({"op": "end", "target": "depot", "at": week_date(2)})
    world.apply({"op": "end", "target": "depot", "at": week_date(3)})
    world.apply({"op": "set", "target": "founded", "value": 1990, "at": week_date(1)})
    world.apply({"op": "end", "target": "founded", "at": week_date(2)})
    with pytest.raises(WorldInvalid) as raised:
        check(world)
    assert {r.code for r in raised.value.refusals} == {
        "one-value-at-a-time",
        "end-of-nothing",
        "permanent-changed",
    }


# -- one failing world for each invariant ---------------------------------------------------------


def test_a_kind_that_the_catalogue_does_not_declare_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "kind": "hunch",
                 "at": week_date(1)})  # fmt: skip
    assert ("undeclared-value", "s") in faults(world)


def test_a_volatility_class_that_the_catalogue_does_not_declare_is_named() -> None:
    world = sound_world()
    world.add_attribute("mood", "office", "fleeting")
    assert ("undeclared-value", "mood") in faults(world)


def test_a_reference_to_nothing_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "no-such-attribute",
                 "kind": "fact", "at": week_date(1)})  # fmt: skip
    assert ("unknown-reference", "s") in faults(world)


def test_a_copy_of_a_statement_that_no_artefact_carries_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "kind": "fact",
                 "at": week_date(1)})  # fmt: skip
    copy_id = world.apply({"op": "copy", "artefact": "s", "at": week_date(2)})
    assert ("unresolved-artefact", copy_id) in faults(world)


def test_a_date_before_the_epoch_is_named() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "approvals", "value": 1, "at": date(2024, 12, 1)})
    assert ("date-before-epoch", "approvals") in faults(world)


def test_two_values_on_one_date_are_named() -> None:
    world = sound_world()
    for value in (1, 2):
        world.apply({"op": "set", "target": "approvals", "value": value, "at": week_date(2)})
    assert ("one-value-at-a-time", "approvals") in faults(world)


def test_an_event_set_at_two_dates_is_named() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "outage", "at": week_date(2)})
    world.apply({"op": "set", "target": "outage", "at": week_date(3)})
    assert ("event-placed-twice", "outage") in faults(world)


def test_ending_an_attribute_that_holds_nothing_is_named() -> None:
    world = sound_world()
    world.apply({"op": "end", "target": "approvals", "at": week_date(2)})
    assert ("end-of-nothing", "approvals") in faults(world)


def test_ending_an_entity_twice_is_named() -> None:
    world = sound_world()
    world.apply({"op": "end", "target": "depot", "at": week_date(2)})
    world.apply({"op": "end", "target": "depot", "at": week_date(3)})
    assert ("end-of-nothing", "depot") in faults(world)


def test_a_permanent_attribute_that_changes_is_named() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "founded", "value": 1990, "at": week_date(1)})
    world.apply({"op": "set", "target": "founded", "value": 1991, "at": week_date(2)})
    assert ("permanent-changed", "founded") in faults(world)


def test_a_value_after_its_entity_ended_is_named() -> None:
    world = sound_world()
    world.apply({"op": "end", "target": "office", "at": week_date(2)})
    world.apply({"op": "set", "target": "approvals", "value": 1, "at": week_date(3)})
    assert ("outside-existence", "approvals") in faults(world)


def test_a_statement_before_its_entity_begins_is_named() -> None:
    world = sound_world()
    world.add_entity("annex", "New annex", week_date(4))
    world.add_attribute("rooms", "annex", "durable")
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "rooms", "value": 3,
                 "kind": "fact", "at": week_date(2)})  # fmt: skip
    assert ("outside-existence", "rooms") in faults(world)


def test_a_name_after_the_entity_was_absorbed_is_named() -> None:
    world = sound_world()
    world.apply({"op": "merge", "entities": ["office", "depot"], "at": week_date(2)})
    world.apply({"op": "name", "entity": "depot", "name": "Annex", "at": week_date(3)})
    assert ("outside-existence", "depot") in faults(world)


def test_a_correction_before_its_statement_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "value": 1,
                 "kind": "fact", "at": week_date(3)})  # fmt: skip
    world.apply({"op": "correct", "by": "lead", "statement": "s", "value": 2, "at": week_date(2)})
    assert ("acts-before-target", "correct s") in faults(world)


def test_an_artefact_dated_before_a_statement_it_carries_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "value": 1,
                 "kind": "fact", "at": week_date(3)})  # fmt: skip
    world.add_artefact("early-note", week_date(1), carries=["s"])
    assert ("acts-before-target", "early-note") in faults(world)


def test_a_statement_by_something_that_is_no_holder_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "office", "about": "approvals", "value": 1,
                 "kind": "fact", "at": week_date(1)})  # fmt: skip
    assert ("not-a-holder", "s") in faults(world)


def test_an_observation_of_an_event_before_it_happens_is_named() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "outage", "at": week_date(3)})
    world.apply({"op": "state", "id": "early", "by": "lead", "about": "outage",
                 "kind": "observation", "at": week_date(2)})  # fmt: skip
    assert ("statement-before-event", "early") in faults(world)


def test_a_plan_for_an_event_before_it_happens_is_no_fault() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "outage", "at": week_date(3)})
    world.apply({"op": "state", "id": "plan", "by": "lead", "about": "outage", "kind": "plan",
                 "at": week_date(2)})  # fmt: skip
    assert validate(world) == []


def test_an_effect_dated_before_its_cause_is_named() -> None:
    world = sound_world()
    world.apply({"op": "set", "target": "outage", "at": week_date(3)})
    world.apply({"op": "set", "target": "restart", "at": week_date(2)})
    world.apply({"op": "cause", "from": "outage", "to": "restart"})
    assert ("effect-before-cause", "cause outage") in faults(world)


def test_a_statement_that_supports_itself_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "kind": "fact",
                 "at": week_date(1)})  # fmt: skip
    world.apply({"op": "support", "from": "s", "to": "s"})
    assert ("edge-to-self", "support s") in faults(world)


def test_a_restriction_that_ends_before_it_begins_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "kind": "fact",
                 "at": week_date(1)})  # fmt: skip
    world.apply({"op": "restrict", "target": "s", "audience": "insiders", "by": "lead",
                 "at": week_date(3), "until": week_date(2)})  # fmt: skip
    assert ("restriction-window", "restrict s") in faults(world)


def test_a_merge_of_one_entity_is_named() -> None:
    world = sound_world()
    world.apply({"op": "merge", "entities": ["office", "office"], "at": week_date(2)})
    assert ("merge-of-fewer-than-two", "merge office, office") in faults(world)


def test_a_statement_that_uses_a_name_its_subject_never_held_is_named() -> None:
    world = sound_world()
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "value": 1,
                 "kind": "fact", "as": "Quay office", "at": week_date(1)})  # fmt: skip
    assert ("name-not-held", "s") in faults(world)


def test_a_name_used_before_it_was_coined_is_named() -> None:
    world = sound_world()
    world.apply({"op": "name", "entity": "office", "name": "Quay office", "at": week_date(4)})
    world.apply({"op": "state", "id": "s", "by": "lead", "about": "approvals", "value": 1,
                 "kind": "fact", "as": "Quay office", "at": week_date(1)})  # fmt: skip
    assert ("name-not-held", "s") in faults(world)


def test_a_draw_whose_chance_is_not_a_chance_rule_is_named() -> None:
    world = sound_world()
    world.apply({"op": "rule", "id": "usual", "by": "lead", "shape": "default", "over": "outage",
                 "value": 0.3, "at": week_date(1)})  # fmt: skip
    world.apply({"op": "draw", "target": "outage", "chance": "usual", "at": week_date(2)})
    assert ("draw-without-chance", "draw outage") in faults(world)


def test_a_context_that_is_its_own_ancestor_is_named() -> None:
    world = sound_world()
    world.add_context("loop-a", parent="loop-b")
    world.add_context("loop-b", parent="loop-a")
    assert ("context-cycle", "loop-a") in faults(world)


def test_no_module_of_the_world_package_imports_the_product() -> None:
    offenders = []
    for path in sorted(WORLD_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] == "exomem"]
    assert offenders == []
