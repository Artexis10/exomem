"""The seven slice 1 mechanisms, each read through a real recipe of the catalogue.

Each case builds the two worlds of a thread from the recipe's own steps and reads the recipe's own
key through ``Oracle.read``. Three oracle checks of design.md decide:

* mutation: the key changes between the planted and the mutation world as the recipe says. A key
  depends on its plant when its value differs, or when it has a trap in the planted world and none
  in the mutation world. The carry rule decides role traps.
* invariance: a statement that the recipe calls irrelevant leaves the key unchanged.
* uniqueness: the reading gives one answer, or the oracle refuses with ``NotUnique``.

The two helpers ``thread`` and ``key`` stand in for the planner of task 3.4 and the key writer of
task 3.6, which replace them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from functools import partial
from typing import Any

import pytest
from membench.clock import week_date
from membench.oracle import lint_claim

from benchmarks.membench.world.core import Vocabulary, World
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.mechanisms import (
    CONTESTED,
    LAST_KNOWN,
    SETTLED,
    WITHDRAWN,
    NoWindow,
    Oracle,
)
from benchmarks.membench.world.record import NotUnique

VOCABULARY = Vocabulary.load()
RECIPES = load_catalogue().recipes

# A step argument that holds a word, not a role, is never replaced by a binding.
_WORDS = ("op", "id", "world", "kind", "basis", "shape", "mode", "hedge")


def week(n: int) -> date:
    return week_date(n)


def new_world(
    holders: tuple[str, ...] = (),
    attributes: tuple[tuple[str, str], ...] = (),
    contexts: tuple[tuple[str, str | None], ...] = (),
) -> World:
    world = World("w", VOCABULARY)
    for holder in holders:
        world.add_entity(holder, holder.title(), week(0), holder=True)
    world.add_entity("office", "Harbour office", week(0))
    for attribute, volatility in attributes:
        world.add_attribute(attribute, "office", volatility)
    for context, parent in contexts:
        world.add_context(context, parent)
    return world


def thread(
    recipe: str, roles: dict[str, Any], slots: dict[str, date], build: Callable[[], World]
) -> tuple[Oracle, Oracle, dict[str, Any]]:
    """The planted and the mutation world of a recipe, as oracles, and the binding that was used."""
    pair = []
    for in_world in ("planted", "mutation"):
        world = build()
        for step in RECIPES[recipe]["steps"]:
            bound = {
                name: roles.get(value, value)
                if name not in _WORDS and isinstance(value, str)
                else [roles.get(v, v) for v in value]
                if isinstance(value, list)
                else value
                for name, value in step.items()
            }
            if "at" in step:
                bound["at"] = slots[step["at"]]
            world.apply(bound, in_world)
        pair.append(Oracle(world, {"transient": timedelta(days=14)}))
    return pair[0], pair[1], {"roles": roles, "slots": slots, "recipe": recipe}


def key(
    oracle: Oracle,
    binding: dict[str, Any],
    form: str,
    twin: bool = False,
    near_miss: bool = False,
) -> tuple[Any, list]:
    """The value and the traps of one probe form of the recipe, as the recipe's key parts read them.

    A trap that equals the value is no trap. A reading trap holds each of its answers. ``twin``
    applies the asserted-content rule of a thread planted in the mutation world, and ``near_miss``
    reads the recipe's `twin` keys, which ask about a case that the plant does not touch.
    """
    recipe = RECIPES[binding["recipe"]]
    spec = (recipe["twin"]["keys"] if near_miss else recipe["keys"])[form]
    roles, at = binding["roles"], binding["slots"][spec["at"]]

    def part(p: dict[str, Any]) -> Any:
        if "role" in p:
            return roles[p["role"]]
        of = p["of"]
        extra = {"where": roles[p["where"]]} if "where" in p else {}
        of = [roles.get(i, i) for i in of] if isinstance(of, list) else roles.get(of, of)
        return oracle.read(p["read"], of=of, at=at, **extra)

    value = part(spec["value"])
    traps: list[Any] = []
    for trap in spec.get("traps", []):
        if "role" in trap:
            if oracle.role_trap(roles[trap["role"]], at, planted_in_mutation=twin):
                traps.append(roles[trap["role"]])
            continue
        answer = part(trap)
        traps += (
            list(answer) if isinstance(answer, tuple | list) else [] if answer is None else [answer]
        )
    return value, [t for t in traps if t != value]


def depends_on_plant(planted: tuple[Any, list], mutation: tuple[Any, list]) -> bool:
    return planted[0] != mutation[0] or (bool(planted[1]) and not mutation[1])


def spans_lint_clean(oracle: Oracle, attribute: str, at: date) -> bool:
    folded = oracle.state_fold.fold(attribute, at)
    known = {f.claim.claim_id for f in folded} | {
        a.source_id for f in folded for a in f.claim.assertions
    }
    return bool(folded) and not [e for f in folded for e in lint_claim(f.claim, frozenset(known))]


# -- state_fold -----------------------------------------------------------------------------------

REVERSAL = {"owner": "lead", "item": "approvals", "old": 1, "new": 2}
REVERSAL_SLOTS = {"before": week(1), "change": week(3), "probe": week(5)}


def reversal_world() -> World:
    return new_world(("lead", "clerk"), (("approvals", "durable"), ("headcount", "durable")))


def test_state_fold_keys_of_a_reversal_depend_on_the_planted_change() -> None:
    planted, mutation, binding = thread(
        "currency.reversal", REVERSAL, REVERSAL_SLOTS, reversal_world
    )

    assert key(planted, binding, "state") == (2, [1])
    assert key(mutation, binding, "state") == (1, [])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))
    assert key(planted, binding, "premise")[0] == 2


def test_state_fold_keeps_the_dates_of_a_reversal_in_its_history() -> None:
    planted, _, _ = thread("currency.reversal", REVERSAL, REVERSAL_SLOTS, reversal_world)

    assert planted.read("state_fold.history", of="approvals", at=week(5)) == (
        (week(1), 1),
        (week(3), 2),
    )


def test_state_fold_ignores_a_report_of_the_old_value_and_an_unrelated_statement() -> None:
    planted, _, binding = thread("currency.reversal", REVERSAL, REVERSAL_SLOTS, reversal_world)
    before = key(planted, binding, "state")

    world = planted.record.world
    world.op_state(
        None, by="clerk", about="approvals", value=1, kind="report", quoting="lead", at=week(4)
    )
    world.op_state(None, by="clerk", about="headcount", value=9, kind="fact", at=week(4))

    assert key(Oracle(world), binding, "state") == before


def test_state_fold_refuses_a_current_value_when_two_holders_disagree_and_no_rule_governs() -> None:
    planted, _, _ = thread("currency.reversal", REVERSAL, REVERSAL_SLOTS, reversal_world)
    planted.record.world.op_state(
        None, by="clerk", about="approvals", value=7, kind="fact", at=week(4)
    )

    with pytest.raises(NotUnique, match="approvals"):
        planted.read("state_fold.current", of="approvals", at=week(5))
    assert planted.read("state_fold.standing", of="approvals", at=week(5)) == CONTESTED


def test_state_fold_lets_a_correction_replace_the_content_for_the_whole_period() -> None:
    roles = {"clerk": "clerk", "user": "boss", "item": "approvals", "wrong": 1, "right": 2}
    slots = {"before": week(1), "corrected": week(3), "probe": week(5)}
    build = partial(new_world, ("clerk", "boss"), (("approvals", "durable"),))
    planted, mutation, binding = thread("correction.user-correction", roles, slots, build)

    assert key(planted, binding, "state") == (2, [1])
    assert key(mutation, binding, "state") == (1, [])
    assert planted.read("state_fold.history", of="approvals", at=week(5)) == ((week(1), 2),)
    assert spans_lint_clean(planted, "approvals", week(5))


def test_state_fold_drops_a_claim_that_a_finding_defeats() -> None:
    roles = {"author": "clerk", "checker": "lead", "item": "approvals", "claimed": 1, "actual": 2}
    slots = {"before": week(1), "refuted": week(3), "probe": week(5)}
    planted, mutation, binding = thread("correction.refuted-claim", roles, slots, reversal_world)

    assert key(planted, binding, "state") == (2, [1])
    assert key(mutation, binding, "state") == (1, [])
    assert spans_lint_clean(planted, "approvals", week(5))


def test_state_fold_follows_the_holder_that_a_stated_rule_governs() -> None:
    roles = {
        "keeper": "lead",
        "registry": "clerk",
        "bystander": "intern",
        "item": "approvals",
        "official": 1,
        "informal": 2,
    }
    slots = {"before": week(1), "entered": week(2), "noted": week(3), "probe": week(5)}
    build = partial(new_world, ("lead", "clerk", "intern"), (("approvals", "durable"),))
    planted, mutation, binding = thread("provenance.primary-over-secondary", roles, slots, build)

    assert key(planted, binding, "state") == (1, [2])
    assert key(mutation, binding, "state") == (1, [])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))


def test_state_fold_reads_a_withdrawn_claim_as_withdrawn_with_its_value_replaced() -> None:
    roles = {"author": "lead", "item": "approvals", "claimed": 1, "actual": 2}
    slots = {"before": week(1), "withdrawal": week(3), "probe": week(5)}
    planted, mutation, binding = thread("correction.retraction", roles, slots, reversal_world)

    assert key(planted, binding, "state") == (WITHDRAWN, [1])
    assert key(mutation, binding, "state") == (SETTLED, [])
    assert planted.read("state_fold.current", of="approvals", at=week(5)) is None


def test_state_fold_reads_an_old_observation_of_a_transient_attribute_as_last_known() -> None:
    roles = {"observer": "clerk", "item": "queue", "passing": 5}
    slots = {"seen": week(1), "ended": week(2), "recent": week(9), "probe": week(10)}
    build = partial(new_world, ("clerk",), (("queue", "transient"),))
    planted, mutation, binding = thread("persistence.transient", roles, slots, build)

    assert key(planted, binding, "state") == (LAST_KNOWN, [5])
    assert key(mutation, binding, "state") == (SETTLED, [5])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))


def test_state_fold_refuses_to_judge_a_stale_class_that_has_no_validity_window() -> None:
    roles = {"observer": "clerk", "item": "queue", "passing": 5}
    slots = {"seen": week(1), "ended": week(2), "recent": week(9), "probe": week(10)}
    build = partial(new_world, ("clerk",), (("queue", "transient"),))
    planted, _, _ = thread("persistence.transient", roles, slots, build)

    with pytest.raises(NoWindow, match="transient"):
        Oracle(planted.record.world).read("state_fold.standing", of="queue", at=week(10))


# -- kind_tag -------------------------------------------------------------------------------------

PLAN = {"planner": "lead", "job": "migration", "done": "done"}
PLAN_SLOTS = {"planned": week(1), "later": week(3), "probe": week(5)}


def plan_world() -> World:
    return new_world(("lead", "clerk"), (("migration", "durable"),))


def test_kind_tag_keeps_a_plan_a_plan_until_an_outcome_is_recorded() -> None:
    planted, mutation, binding = thread("kind.plan-not-done", PLAN, PLAN_SLOTS, plan_world)

    assert key(planted, binding, "state") == ("plan", ["done"])
    assert key(mutation, binding, "state") == ("result", ["done"])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))


def test_kind_tag_of_a_thread_planted_in_the_mutation_world_drops_the_role_trap_that_stands() -> (
    None
):
    _, mutation, binding = thread("kind.plan-not-done", PLAN, PLAN_SLOTS, plan_world)

    assert key(mutation, binding, "state", twin=True) == ("result", [])


def test_kind_tag_ignores_an_unrelated_statement() -> None:
    planted, _, binding = thread("kind.plan-not-done", PLAN, PLAN_SLOTS, plan_world)
    planted.record.world.op_state(
        None, by="clerk", about="other", kind="result", value="done", at=week(4)
    )

    assert key(Oracle(planted.record.world), binding, "state") == ("plan", ["done"])


def test_kind_tag_lets_the_latest_asserting_statement_decide_over_a_later_proposal() -> None:
    _, mutation, _ = thread("kind.plan-not-done", PLAN, PLAN_SLOTS, plan_world)
    mutation.record.world.op_state(
        None, by="lead", about="migration", kind="plan", value="done", at=week(4)
    )

    assert mutation.read("kind_tag.kind", of="migration", at=week(5)) == "result"


def test_kind_tag_reads_the_hedge_that_a_statement_carries() -> None:
    roles = {"speaker": "lead", "item": "approvals", "claimed": 3}
    slots = {"said": week(1), "probe": week(5)}
    planted, mutation, binding = thread("uncertainty.hedged-claim", roles, slots, reversal_world)

    assert key(planted, binding, "state") == ("tentative", [3])
    assert key(mutation, binding, "state") == (None, [3])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))


# -- holder_ledger --------------------------------------------------------------------------------


def nested_report() -> tuple[Oracle, Oracle, dict[str, Any]]:
    roles = {"speaker": "lead", "reporter": "clerk", "item": "approvals", "claimed": 3}
    slots = {"told": week(1), "probe": week(5)}
    return thread("attribution.nested-report", roles, slots, reversal_world)


def test_holder_ledger_gives_a_quoted_report_back_to_the_holder_it_quotes() -> None:
    planted, mutation, binding = nested_report()

    assert key(planted, binding, "whose") == ("lead", ["clerk"])
    assert key(mutation, binding, "whose") == ("lead", [])
    assert depends_on_plant(key(planted, binding, "whose"), key(mutation, binding, "whose"))
    assert key(planted, binding, "act") == ((), [3])
    assert key(mutation, binding, "act") == (("lead",), [3])


def test_holder_ledger_keeps_the_holder_when_an_unrelated_holder_speaks() -> None:
    planted, _, binding = nested_report()
    planted.record.world.op_state(
        None, by="clerk", about="headcount", value=3, kind="fact", at=week(2)
    )

    assert key(Oracle(planted.record.world), binding, "whose") == ("lead", ["clerk"])


def test_holder_ledger_follows_a_told_statement_back_to_the_person_who_said_it_first() -> None:
    roles = {"person": "lead", "writer": "clerk", "item": "approvals", "view": 3}
    slots = {"said": week(1), "restated": week(3), "probe": week(5)}
    planted, mutation, binding = thread(
        "attribution.restated-as-fact", roles, slots, reversal_world
    )

    assert key(planted, binding, "whose") == ("lead", ["clerk"])
    assert key(mutation, binding, "whose") == ("lead", [])
    assert depends_on_plant(key(planted, binding, "whose"), key(mutation, binding, "whose"))


def test_holder_ledger_refuses_a_told_statement_whose_telling_is_not_recorded() -> None:
    world = reversal_world()
    world.op_state(
        "note", by="clerk", about="approvals", value=3, kind="fact", basis="told", at=week(1)
    )

    with pytest.raises(NotUnique, match="note"):
        Oracle(world).read("holder_ledger.holder", of="approvals", at=week(5))


def test_holder_ledger_refuses_a_holder_of_an_attribute_that_carries_two_contents() -> None:
    world = reversal_world()
    world.op_state(None, by="lead", about="approvals", value=1, kind="fact", at=week(1))
    world.op_state(None, by="clerk", about="approvals", value=2, kind="fact", at=week(2))

    with pytest.raises(NotUnique, match="approvals"):
        Oracle(world).read("holder_ledger.holder", of="approvals", at=week(5))
    assert Oracle(world).read("holder_ledger.holder", of="approvals", at=week(5), for_=2) == "clerk"


# -- identity_map ---------------------------------------------------------------------------------


def rename_world() -> World:
    world = new_world()
    world.add_entity("depot", "North depot", week(0))
    world.add_entity("yard", "South yard", week(0))
    return world


def test_identity_map_gives_a_new_name_to_its_entity_only_after_the_rename() -> None:
    roles = {"subject": "depot", "former": "North depot", "fresh": "Quay depot"}
    slots = {"renamed": week(2), "probe": week(5)}
    planted, mutation, binding = thread("identity.rename", roles, slots, rename_world)

    assert key(planted, binding, "state")[0] == "depot"
    assert key(mutation, binding, "state")[0] is None
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))
    assert planted.read("identity_map.names", of="depot", at=week(5)) == (
        "North depot",
        "Quay depot",
    )
    assert planted.read("identity_map.referent", of="North depot", at=week(5)) is None


def test_identity_map_sends_the_name_of_an_absorbed_entity_to_the_survivor() -> None:
    roles = {"first": "depot", "second": "yard", "absorbed": "South yard", "joint": "Quay site"}
    slots = {"merged": week(2), "probe": week(5)}
    planted, mutation, binding = thread("identity.merge", roles, slots, rename_world)

    assert key(planted, binding, "state")[0] == "depot"
    assert key(mutation, binding, "state")[0] == "yard"
    assert planted.read("identity_map.names", of="depot", at=week(5)) == (
        "North depot",
        "Quay site",
        "South yard",
    )


def test_identity_map_leaves_a_key_unchanged_when_another_entity_is_renamed() -> None:
    roles = {"subject": "depot", "former": "North depot", "fresh": "Quay depot"}
    slots = {"renamed": week(2), "probe": week(5)}
    planted, _, binding = thread("identity.rename", roles, slots, rename_world)
    planted.record.world.op_name(None, entity="yard", name="East yard", at=week(3))

    assert key(Oracle(planted.record.world), binding, "state")[0] == "depot"


def test_identity_map_refuses_a_name_that_two_entities_hold() -> None:
    roles = {"subject": "depot", "short": "Quay"}
    slots = {"coined": week(2), "probe": week(5)}
    planted, _, _ = thread("identity.alias", roles, slots, rename_world)
    planted.record.world.op_name(None, entity="yard", name="Quay", at=week(3))

    with pytest.raises(NotUnique, match="Quay"):
        planted.read("identity_map.referent", of="Quay", at=week(5))


# -- scope_lattice --------------------------------------------------------------------------------

SCOPES = (("everywhere", None), ("quay", "everywhere"), ("yard", "everywhere"))


def scope_world() -> World:
    return new_world(("lead",), (("limit", "durable"),), SCOPES)


EXCEPTION_ROLES = {
    "owner": "lead",
    "item": "limit",
    "usual": 10,
    "special": 4,
    "everywhere": "everywhere",
    "here": "quay",
    "there": "yard",
}
EXCEPTION_SLOTS = {"before": week(1), "probe": week(5)}


def test_scope_lattice_lets_the_exception_decide_in_its_own_context_and_the_default_elsewhere() -> (
    None
):
    planted, mutation, binding = thread(
        "scope.exception", EXCEPTION_ROLES, EXCEPTION_SLOTS, scope_world
    )

    assert key(planted, binding, "state") == (4, [10])
    assert key(mutation, binding, "state") == (10, [])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))


def test_scope_lattice_keeps_the_default_for_a_case_that_the_exception_does_not_cover() -> None:
    planted, _, binding = thread("scope.exception", EXCEPTION_ROLES, EXCEPTION_SLOTS, scope_world)

    assert key(planted, binding, "state", near_miss=True) == (10, [4])


def test_scope_lattice_keeps_the_key_when_a_decision_is_made_in_an_unrelated_context() -> None:
    planted, _, binding = thread("scope.exception", EXCEPTION_ROLES, EXCEPTION_SLOTS, scope_world)
    planted.record.world.op_state(
        None, by="lead", about="limit", value=99, kind="decision", scope="yard", at=week(2)
    )

    assert key(Oracle(planted.record.world), binding, "state") == (4, [10])


def test_scope_lattice_holds_a_decision_in_its_own_context_and_not_beyond() -> None:
    roles = {
        "owner": "lead",
        "item": "limit",
        "special": 4,
        "everywhere": "everywhere",
        "here": "quay",
        "there": "yard",
    }
    slots = {"before": week(1), "probe": week(5)}
    planted, mutation, binding = thread("scope.one-client", roles, slots, scope_world)

    assert key(planted, binding, "act") == (None, [4])
    assert key(mutation, binding, "act") == (4, [])
    assert planted.read("scope_lattice.applies", of="local", at=week(5), where="yard") is False
    assert mutation.read("scope_lattice.applies", of="local", at=week(5), where="yard") is True


# -- warrant_graph --------------------------------------------------------------------------------


def plan_invalidated() -> tuple[Oracle, Oracle, dict[str, Any]]:
    roles = {
        "owner": "lead",
        "planner": "clerk",
        "base": "approvals",
        "old": 1,
        "new": 2,
        "job": "release",
        "intended": "ship",
        "apart": "audit",
        "kept": "later",
    }
    slots = {"before": week(1), "change": week(3), "probe": week(5)}
    build = partial(
        new_world,
        ("lead", "clerk"),
        (("approvals", "durable"), ("release", "durable"), ("audit", "durable")),
    )
    return thread("propagation.plan-invalidated", roles, slots, build)


def test_warrant_graph_drops_a_plan_whose_only_support_was_replaced() -> None:
    planted, mutation, binding = plan_invalidated()

    assert key(planted, binding, "state") == (False, ["scheme"])
    assert key(mutation, binding, "state") == (True, [])
    assert depends_on_plant(key(planted, binding, "state"), key(mutation, binding, "state"))
    assert planted.read("warrant_graph.stands", of="aside", at=week(5)) is True


def test_warrant_graph_keeps_a_conclusion_that_has_another_support_when_one_is_withdrawn() -> None:
    roles = {
        "author": "lead",
        "analyst": "clerk",
        "first_sign": "gauge",
        "seen_first": 1,
        "second_sign": "meter",
        "seen_second": 2,
        "verdict": "approvals",
        "held": 3,
        "lone": "release",
        "lost": 4,
    }
    slots = {"before": week(1), "change": week(3), "probe": week(5)}
    build = partial(
        new_world,
        ("lead", "clerk"),
        (
            ("gauge", "durable"),
            ("meter", "durable"),
            ("approvals", "durable"),
            ("release", "durable"),
        ),
    )
    planted, mutation, binding = thread("propagation.independent-support", roles, slots, build)

    assert key(planted, binding, "act") == ((True, False), ["single"])
    assert planted.read("warrant_graph.surviving", of="one", at=week(5)) == ("conclusion",)
    assert planted.read("warrant_graph.fallen", of="one", at=week(5)) == ("single",)
    assert key(mutation, binding, "act")[0] == (True, True)
    assert key(mutation, binding, "impact")[0] == ()


def test_warrant_graph_lists_the_statements_for_a_claim_and_against_it() -> None:
    roles = {
        "proponent": "lead",
        "opponent": "clerk",
        "item": "approvals",
        "claimed": 1,
        "opposite": 2,
        "sign": "gauge",
        "seen": 5,
    }
    slots = {"before": week(1), "change": week(3), "probe": week(5)}
    build = partial(new_world, ("lead", "clerk"), (("approvals", "durable"), ("gauge", "durable")))
    planted, mutation, binding = thread("argument.rebuttal", roles, slots, build)

    assert key(planted, binding, "weigh") == ((("ground",), ("counter",)), [1])
    assert key(mutation, binding, "weigh")[0] == (("ground",), ())
    assert key(planted, binding, "trace") == (("counter",), ["ground"])


def test_warrant_graph_counts_statements_that_trace_to_one_origin_as_one() -> None:
    roles = {
        "witness": "lead",
        "relay": "clerk",
        "echo": "intern",
        "item": "approvals",
        "claimed": 1,
    }
    slots = {"seen": week(1), "retold": week(3), "probe": week(5)}
    build = partial(new_world, ("lead", "clerk", "intern"), (("approvals", "durable"),))
    planted, mutation, binding = thread("provenance.circular-reporting", roles, slots, build)

    assert key(planted, binding, "trace")[0] == ("lead",)
    assert key(mutation, binding, "trace")[0] == ("lead", "intern")
    assert planted.read("holder_ledger.holders", of="approvals", at=week(5)) == (
        "lead",
        "clerk",
        "intern",
    )


# -- corpus_closure -------------------------------------------------------------------------------


def test_corpus_closure_does_not_count_a_question_as_a_record() -> None:
    roles = {"asker": "lead", "answerer": "clerk", "item": "approvals", "answer": 3}
    slots = {"asked": week(1), "later": week(3), "probe": week(5)}
    planted, mutation, binding = thread("uncertainty.open-question", roles, slots, reversal_world)

    assert key(planted, binding, "abstain")[0] is False
    assert key(mutation, binding, "abstain")[0] is True
    assert depends_on_plant(key(planted, binding, "abstain"), key(mutation, binding, "abstain"))


def test_corpus_closure_finds_nothing_recorded_for_a_value_that_only_the_world_holds() -> None:
    roles = {"witness": "lead", "item": "approvals", "actual": 3}
    slots = {"before": week(1), "probe": week(5)}
    planted, mutation, binding = thread("absence.not-recorded", roles, slots, reversal_world)

    assert key(planted, binding, "abstain")[0] is False
    assert key(mutation, binding, "abstain")[0] is True


def test_corpus_closure_ignores_a_statement_about_another_subject() -> None:
    roles = {"witness": "lead", "item": "approvals", "actual": 3}
    slots = {"before": week(1), "probe": week(5)}
    planted, _, binding = thread("absence.not-recorded", roles, slots, reversal_world)
    planted.record.world.op_state(
        None, by="clerk", about="headcount", value=3, kind="fact", at=week(2)
    )

    assert key(Oracle(planted.record.world), binding, "abstain")[0] is False
