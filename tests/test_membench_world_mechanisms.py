"""The seven slice 1 mechanisms, each read through a real recipe of the catalogue.

Each case lets the planner plant one real recipe and read the recipe's own key through
``Oracle.read``, in the thread's own two worlds. Three oracle checks of design.md decide:

* mutation: the key changes between the planted and the mutation world as the recipe says. A key
  depends on its plant when its value differs, or when it has a trap in the planted world and none
  in the mutation world. The carry rule decides role traps.
* invariance: a statement that the recipe calls irrelevant leaves the key unchanged.
* uniqueness: the reading gives one answer, or the oracle refuses with ``NotUnique``.

The planner binds each recipe's roles, so a case reads each value from the thread and never fixes it.
``written`` reads the key that the key writer writes (``keys.write_key``).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pytest
from membench.clock import week_date
from membench.oracle import lint_claim

from benchmarks.membench.world import core
from benchmarks.membench.world.core import Vocabulary, World
from benchmarks.membench.world.keys import write_key
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.mechanisms import (
    CONTESTED,
    LAST_KNOWN,
    SETTLED,
    WITHDRAWN,
    NoWindow,
    Oracle,
)
from benchmarks.membench.world.pack import PACKS_DIR, load_pack
from benchmarks.membench.world.planner import PLANTED, Plan, RoundSpec, Thread, plan_world
from benchmarks.membench.world.record import NotUnique

VOCABULARY = Vocabulary.load()
CATALOGUE = load_catalogue(implemented=core.implemented())
PACK = load_pack(PACKS_DIR / "operations.yaml")
SEED = 11
# A value that no attribute of the pack takes, for a statement that must not match a role.
OUTSIDE = -1
DAY = timedelta(days=1)


def plan_of(*entries: str) -> Plan:
    return plan_world(RoundSpec(entries, twins=0), CATALOGUE, PACK, VOCABULARY, "dev", SEED)


def plant(entry: str) -> tuple[Oracle, Oracle, Thread]:
    """The planted and the mutation world of one thread, as oracles, and the thread."""
    plan = plan_of(entry)
    (thread,) = plan.threads
    assert thread.variant == PLANTED and not plan.drops
    planted, mutation = (Oracle(world, plan.validity()) for world in plan.pair(thread))
    return planted, mutation, thread


def bare_world() -> tuple[World, Plan]:
    """A world that holds the backbone and no thread."""
    plan = plan_of()
    world = World(plan.name, VOCABULARY)
    plan.backbone.declare(world)
    return world, plan


def holder_outside(oracle: Oracle, thread: Thread) -> str:
    """A holder that no role of the thread binds."""
    return next(h for h in oracle.record.world.holders() if h not in thread.roles.values())


def attribute_outside(oracle: Oracle, thread: Thread) -> str:
    """An attribute that no thread reserves."""
    world = oracle.record.world
    return next(a for a in sorted(world.attributes) if a not in thread.reserved)


def written(oracle: Oracle, thread: Thread, form: str, twin: bool = False) -> tuple[Any, list]:
    """The value and the traps of the key that the writer writes for one probe form of a thread.
    ``twin`` says that the thread is planted in the mutation world."""
    key = write_key(oracle, thread, CATALOGUE, form, twin=twin)
    return key.value, list(key.traps)


def depends_on_plant(planted: tuple[Any, list], mutation: tuple[Any, list]) -> bool:
    return planted[0] != mutation[0] or (bool(planted[1]) and not mutation[1])


def spans_lint_clean(oracle: Oracle, attribute: str, at: date) -> bool:
    folded = oracle.state_fold.fold(attribute, at)
    known = {f.claim.claim_id for f in folded} | {
        a.source_id for f in folded for a in f.claim.assertions
    }
    return bool(folded) and not [e for f in folded for e in lint_claim(f.claim, frozenset(known))]


# -- state_fold -----------------------------------------------------------------------------------


def test_state_fold_keys_of_a_reversal_depend_on_the_planted_change() -> None:
    planted, mutation, t = plant("currency.reversal")
    old, new = t.roles["old"], t.roles["new"]

    assert written(planted, t, "state") == (new, [old])
    assert written(mutation, t, "state") == (old, [])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))
    assert written(planted, t, "premise")[0] == new


def test_state_fold_keeps_the_dates_of_a_reversal_in_its_history() -> None:
    planted, _, t = plant("currency.reversal")

    assert planted.read("state_fold.history", of=t.roles["item"], at=t.slots["probe"]) == (
        (t.slots["before"], t.roles["old"]),
        (t.slots["change"], t.roles["new"]),
    )


def test_state_fold_ignores_a_report_of_the_old_value_and_an_unrelated_statement() -> None:
    planted, _, t = plant("currency.reversal")
    before = written(planted, t, "state")
    world = planted.record.world
    bystander, other = holder_outside(planted, t), attribute_outside(planted, t)

    after_change = t.slots["change"] + DAY
    world.op_state(
        None,
        by=bystander,
        about=t.roles["item"],
        value=t.roles["old"],
        kind="report",
        quoting=t.roles["owner"],
        at=after_change,
    )
    world.op_state(None, by=bystander, about=other, value=OUTSIDE, kind="fact", at=after_change)

    assert written(Oracle(world, {}), t, "state") == before


def test_state_fold_refuses_a_current_value_when_two_holders_disagree_and_no_rule_governs() -> None:
    planted, _, t = plant("currency.reversal")
    item, probe = t.roles["item"], t.slots["probe"]
    planted.record.world.op_state(
        None,
        by=holder_outside(planted, t),
        about=item,
        value=OUTSIDE,
        kind="fact",
        at=t.slots["change"] + DAY,
    )

    with pytest.raises(NotUnique, match=re.escape(item)):
        planted.read("state_fold.current", of=item, at=probe)
    assert planted.read("state_fold.standing", of=item, at=probe) == CONTESTED


def test_state_fold_lets_a_correction_replace_the_content_for_the_whole_period() -> None:
    planted, mutation, t = plant("correction.user-correction")
    item, wrong, right = t.roles["item"], t.roles["wrong"], t.roles["right"]

    assert written(planted, t, "state") == (right, [wrong])
    assert written(mutation, t, "state") == (wrong, [])
    assert planted.read("state_fold.history", of=item, at=t.slots["probe"]) == (
        (t.slots["before"], right),
    )
    assert spans_lint_clean(planted, item, t.slots["probe"])


def test_state_fold_drops_a_claim_that_a_finding_defeats() -> None:
    planted, mutation, t = plant("correction.refuted-claim")

    assert written(planted, t, "state") == (t.roles["actual"], [t.roles["claimed"]])
    assert written(mutation, t, "state") == (t.roles["claimed"], [])
    assert spans_lint_clean(planted, t.roles["item"], t.slots["probe"])


def test_state_fold_follows_the_holder_that_a_stated_rule_governs() -> None:
    planted, mutation, t = plant("provenance.primary-over-secondary")
    official, informal = t.roles["official"], t.roles["informal"]

    assert written(planted, t, "state") == (official, [informal])
    assert written(mutation, t, "state") == (official, [])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))


def test_state_fold_reads_a_withdrawn_claim_as_withdrawn_with_its_value_replaced() -> None:
    planted, mutation, t = plant("correction.retraction")
    claimed = t.roles["claimed"]

    assert written(planted, t, "state") == (WITHDRAWN, [claimed])
    assert written(mutation, t, "state") == (SETTLED, [])
    assert planted.read("state_fold.current", of=t.roles["item"], at=t.slots["probe"]) is None


def test_state_fold_reads_an_old_observation_of_a_transient_attribute_as_last_known() -> None:
    planted, mutation, t = plant("persistence.transient")
    passing = t.roles["passing"]

    assert written(planted, t, "state") == (LAST_KNOWN, [passing])
    assert written(mutation, t, "state") == (SETTLED, [passing])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))


def test_state_fold_refuses_to_judge_a_stale_class_that_has_no_validity_window() -> None:
    planted, _, t = plant("persistence.transient")

    with pytest.raises(NoWindow, match="transient"):
        Oracle(planted.record.world).read(
            "state_fold.standing", of=t.roles["item"], at=t.slots["probe"]
        )


# -- kind_tag -------------------------------------------------------------------------------------


def test_kind_tag_keeps_a_plan_a_plan_until_an_outcome_is_recorded() -> None:
    planted, mutation, t = plant("kind.plan-not-done")
    done = t.roles["done"]

    assert written(planted, t, "state") == ("plan", [done])
    assert written(mutation, t, "state") == ("result", [done])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))


def test_kind_tag_of_a_thread_planted_in_the_mutation_world_drops_the_role_trap_that_stands() -> (
    None
):
    _, mutation, t = plant("kind.plan-not-done")

    assert written(mutation, t, "state", twin=True) == ("result", [])


def test_kind_tag_ignores_an_unrelated_statement() -> None:
    planted, _, t = plant("kind.plan-not-done")
    planted.record.world.op_state(
        None,
        by=t.roles["planner"],
        about=attribute_outside(planted, t),
        kind="result",
        value=OUTSIDE,
        at=t.slots["later"],
    )

    assert written(Oracle(planted.record.world), t, "state") == ("plan", [t.roles["done"]])


def test_kind_tag_lets_the_latest_asserting_statement_decide_over_a_later_proposal() -> None:
    _, mutation, t = plant("kind.plan-not-done")
    mutation.record.world.op_state(
        None,
        by=t.roles["planner"],
        about=t.roles["job"],
        kind="plan",
        value=t.roles["done"],
        at=t.slots["later"] + DAY,
    )

    assert mutation.read("kind_tag.kind", of=t.roles["job"], at=t.slots["probe"]) == "result"


def test_kind_tag_reads_the_hedge_that_a_statement_carries() -> None:
    planted, mutation, t = plant("uncertainty.hedged-claim")
    claimed = t.roles["claimed"]

    assert written(planted, t, "state") == ("tentative", [claimed])
    assert written(mutation, t, "state") == (None, [claimed])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))


# -- holder_ledger --------------------------------------------------------------------------------


def test_holder_ledger_gives_a_quoted_report_back_to_the_holder_it_quotes() -> None:
    planted, mutation, t = plant("attribution.nested-report")
    speaker, reporter, claimed = t.roles["speaker"], t.roles["reporter"], t.roles["claimed"]

    assert written(planted, t, "whose") == (speaker, [reporter])
    assert written(mutation, t, "whose") == (speaker, [])
    assert depends_on_plant(written(planted, t, "whose"), written(mutation, t, "whose"))
    assert written(planted, t, "act") == ((), [claimed])
    assert written(mutation, t, "act") == ((speaker,), [claimed])


def test_holder_ledger_keeps_the_holder_when_an_unrelated_holder_speaks() -> None:
    planted, _, t = plant("attribution.nested-report")
    planted.record.world.op_state(
        None,
        by=t.roles["reporter"],
        about=attribute_outside(planted, t),
        value=OUTSIDE,
        kind="fact",
        at=t.slots["told"] + DAY,
    )

    assert written(Oracle(planted.record.world), t, "whose") == (
        t.roles["speaker"],
        [t.roles["reporter"]],
    )


def test_holder_ledger_follows_a_told_statement_back_to_the_person_who_said_it_first() -> None:
    planted, mutation, t = plant("attribution.restated-as-fact")

    assert written(planted, t, "whose") == (t.roles["person"], [t.roles["writer"]])
    assert written(mutation, t, "whose") == (t.roles["person"], [])
    assert depends_on_plant(written(planted, t, "whose"), written(mutation, t, "whose"))


def test_holder_ledger_refuses_a_told_statement_whose_telling_is_not_recorded() -> None:
    world, plan = bare_world()
    (holder, *_), attribute = plan.backbone.holders(), sorted(plan.backbone.attributes)[0]
    world.op_state(
        "note", by=holder, about=attribute, value=3, kind="fact", basis="told", at=week_date(1)
    )

    with pytest.raises(NotUnique, match="note"):
        Oracle(world).read("holder_ledger.holder", of=attribute, at=week_date(5))


def test_holder_ledger_refuses_a_holder_of_an_attribute_that_carries_two_contents() -> None:
    world, plan = bare_world()
    (first, second, *_), attribute = plan.backbone.holders(), sorted(plan.backbone.attributes)[0]
    world.op_state(None, by=first, about=attribute, value=1, kind="fact", at=week_date(1))
    world.op_state(None, by=second, about=attribute, value=2, kind="fact", at=week_date(2))

    with pytest.raises(NotUnique, match=re.escape(attribute)):
        Oracle(world).read("holder_ledger.holder", of=attribute, at=week_date(5))
    assert (
        Oracle(world).read("holder_ledger.holder", of=attribute, at=week_date(5), for_=2) == second
    )


# -- identity_map ---------------------------------------------------------------------------------


def test_identity_map_gives_a_new_name_to_its_entity_only_after_the_rename() -> None:
    planted, mutation, t = plant("identity.rename")
    subject, former, fresh = t.roles["subject"], t.roles["former"], t.roles["fresh"]
    probe = t.slots["probe"]

    assert written(planted, t, "state")[0] == subject
    assert written(mutation, t, "state")[0] is None
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))
    assert planted.read("identity_map.names", of=subject, at=probe) == (former, fresh)
    assert planted.read("identity_map.referent", of=former, at=probe) is None


def test_identity_map_sends_the_name_of_an_absorbed_entity_to_the_survivor() -> None:
    planted, mutation, t = plant("identity.merge")
    first, second = t.roles["first"], t.roles["second"]
    probe = t.slots["probe"]

    assert written(planted, t, "state")[0] == first
    assert written(mutation, t, "state")[0] == second
    held = planted.read("identity_map.names", of=first, at=probe)
    assert sorted(held) == sorted(
        [
            planted.record.world.entities[first].name,
            t.roles["joint"],
            t.roles["absorbed"],
        ]
    )


def test_identity_map_leaves_a_key_unchanged_when_another_entity_is_renamed() -> None:
    planted, _, t = plant("identity.rename")
    world = planted.record.world
    other = next(e for e in world.entities.values() if not e.holder and e.id not in t.reserved)
    world.op_name(None, entity=other.id, name="Unrelated name", at=t.slots["renamed"] + DAY)

    assert written(Oracle(world), t, "state")[0] == t.roles["subject"]


def test_identity_map_refuses_a_name_that_two_entities_hold() -> None:
    planted, _, t = plant("identity.alias")
    world = planted.record.world
    other = next(e for e in world.entities.values() if not e.holder and e.id not in t.reserved)
    world.op_name(None, entity=other.id, name=t.roles["short"], at=t.slots["coined"] + DAY)

    with pytest.raises(NotUnique, match=re.escape(t.roles["short"])):
        planted.read("identity_map.referent", of=t.roles["short"], at=t.slots["probe"])


# -- scope_lattice --------------------------------------------------------------------------------


def test_scope_lattice_lets_the_exception_decide_in_its_own_context_and_the_default_elsewhere() -> (
    None
):
    planted, mutation, t = plant("scope.exception")
    usual, special = t.roles["usual"], t.roles["special"]

    assert written(planted, t, "state") == (special, [usual])
    assert written(mutation, t, "state") == (usual, [])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))


def test_scope_lattice_keeps_the_default_for_a_case_that_the_exception_does_not_cover() -> None:
    planted, _, t = plant("scope.general-rule-applies")

    assert written(planted, t, "state") == (t.roles["usual"], [t.roles["special"]])


def test_scope_lattice_keeps_the_key_when_a_decision_is_made_in_an_unrelated_context() -> None:
    plan = plan_of("scope.exception")
    (t,) = plan.threads
    planted, _ = (Oracle(w, plan.validity()) for w in plan.pair(t))
    siblings = plan.backbone.children_of(t.roles["everywhere"])
    unrelated = next(c for c in siblings if c not in (t.roles["here"], t.roles["there"]))
    planted.record.world.op_state(
        None,
        by=t.roles["owner"],
        about=t.roles["item"],
        value=OUTSIDE,
        kind="decision",
        scope=unrelated,
        at=t.slots["before"] + DAY,
    )

    assert written(Oracle(planted.record.world), t, "state") == (
        t.roles["special"],
        [t.roles["usual"]],
    )


def test_scope_lattice_holds_a_decision_in_its_own_context_and_not_beyond() -> None:
    planted, mutation, t = plant("scope.one-client")
    special, probe = t.roles["special"], t.slots["probe"]
    local, there = t.resolve("local"), t.roles["there"]

    assert written(planted, t, "act") == (None, [special])
    assert written(mutation, t, "act") == (special, [])
    assert planted.read("scope_lattice.applies", of=local, at=probe, where=there) is False
    assert mutation.read("scope_lattice.applies", of=local, at=probe, where=there) is True


# -- warrant_graph --------------------------------------------------------------------------------


def test_warrant_graph_drops_a_plan_whose_only_support_was_replaced() -> None:
    planted, mutation, t = plant("propagation.plan-invalidated")

    assert written(planted, t, "state") == (False, [t.resolve("scheme")])
    assert written(mutation, t, "state") == (True, [])
    assert depends_on_plant(written(planted, t, "state"), written(mutation, t, "state"))
    assert planted.read("warrant_graph.stands", of=t.resolve("aside"), at=t.slots["probe"]) is True


def test_warrant_graph_keeps_a_conclusion_that_has_another_support_when_one_is_withdrawn() -> None:
    planted, mutation, t = plant("propagation.independent-support")
    probe = t.slots["probe"]

    assert written(planted, t, "act") == ((True, False), [t.resolve("single")])
    assert planted.read("warrant_graph.surviving", of=t.resolve("one"), at=probe) == (
        t.resolve("conclusion"),
    )
    assert planted.read("warrant_graph.fallen", of=t.resolve("one"), at=probe) == (
        t.resolve("single"),
    )
    assert written(mutation, t, "act")[0] == (True, True)
    assert written(mutation, t, "impact")[0] == ()


def test_warrant_graph_lists_the_statements_for_a_claim_and_against_it() -> None:
    planted, mutation, t = plant("argument.rebuttal")
    ground, counter = t.resolve("ground"), t.resolve("counter")

    assert written(planted, t, "weigh") == (((ground,), (counter,)), [t.roles["claimed"]])
    assert written(mutation, t, "weigh")[0] == ((ground,), ())
    assert written(planted, t, "trace") == ((counter,), [ground])


def test_warrant_graph_counts_statements_that_trace_to_one_origin_as_one() -> None:
    planted, mutation, t = plant("provenance.circular-reporting")
    witness, relay, echo = t.roles["witness"], t.roles["relay"], t.roles["echo"]

    assert written(planted, t, "trace")[0] == (witness,)
    assert written(mutation, t, "trace")[0] == (witness, echo)
    assert planted.read("holder_ledger.holders", of=t.roles["item"], at=t.slots["probe"]) == (
        witness,
        relay,
        echo,
    )


# -- corpus_closure -------------------------------------------------------------------------------


def test_corpus_closure_does_not_count_a_question_as_a_record() -> None:
    planted, mutation, t = plant("uncertainty.open-question")

    assert written(planted, t, "abstain")[0] is False
    assert written(mutation, t, "abstain")[0] is True
    assert depends_on_plant(written(planted, t, "abstain"), written(mutation, t, "abstain"))


def test_corpus_closure_finds_nothing_recorded_for_a_value_that_only_the_world_holds() -> None:
    planted, mutation, t = plant("absence.not-recorded")

    assert written(planted, t, "abstain")[0] is False
    assert written(mutation, t, "abstain")[0] is True


def test_corpus_closure_ignores_a_statement_about_another_subject() -> None:
    planted, _, t = plant("absence.not-recorded")
    planted.record.world.op_state(
        None,
        by=holder_outside(planted, t),
        about=attribute_outside(planted, t),
        value=OUTSIDE,
        kind="fact",
        at=t.slots["before"] + DAY,
    )

    assert written(Oracle(planted.record.world), t, "abstain")[0] is False
