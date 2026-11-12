"""The key writer of the synthetic world: each probe's key, proof trace and contest set.

The first case is the workflow: generate the seed-7 development world, write it with its keys, and
read each probe's key, its proof trace and its contest set from the key file. The next two are the
checks of the task: a thread with a replaced value has the document that carries the replaced value
in its contest set, and a key that rests on a stated norm has the document that states it in its
proof trace. The rest break one rule each: a value that two threads share, a norm that no document
states, a reading that is not built, a premise that is not one state, and a reading that two holders
leave open.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from benchmarks.membench.world import core
from benchmarks.membench.world.core import Vocabulary
from benchmarks.membench.world.keys import KeyRefused, keys_digest, write_key, write_keys
from benchmarks.membench.world.loader import load_catalogue
from benchmarks.membench.world.mechanisms import Oracle
from benchmarks.membench.world.pack import PACKS_DIR, load_pack
from benchmarks.membench.world.planner import MUTATION, Plan, RoundSpec, Thread, plan_world
from benchmarks.membench.world.render import render
from benchmarks.membench.world.surface import STATED, load_bank

VOCABULARY = Vocabulary.load()
CATALOGUE = load_catalogue(implemented=core.implemented())
PACK = load_pack(PACKS_DIR / "operations.yaml")
BANK = load_bank(PACKS_DIR / "operations.dev.forms.yaml", VOCABULARY, PACK)
SLICE_ONE = tuple(i for i in CATALOGUE.buildable if CATALOGUE.entries[i].stage == "s1")


def plan_of(*entries: str, seed: int = 11, twins: float = 0) -> Plan:
    spec = RoundSpec(entries, twins=twins)
    return plan_world(spec, CATALOGUE, PACK, VOCABULARY, "dev", seed)


def keyed(entry: str, form: str, **plan: Any) -> tuple[Plan, Thread, Any]:
    """The plan of one thread, the thread, and its key for one probe form."""
    p = plan_of(entry, **plan)
    (thread,) = p.threads
    (key,) = (k for k in write_keys(p, CATALOGUE) if k.form == form)
    return p, thread, key


def carriers(plan: Plan, thread: Thread, step: str) -> set[str]:
    """The documents that carry a statement of the thread, or announce an operation of it."""
    ref = thread.step_ids[step]
    world = plan.world()
    return {a.id for a in world.carriers(ref)} | {
        a.id for a in world.artefacts.values() if a.announces == ref
    }


def refusal(plan: Plan, catalogue: Any = CATALOGUE) -> set[str]:
    with pytest.raises(KeyRefused) as refused:
        write_keys(plan, catalogue)
    return {r.code for r in refused.value.refusals}


# -- the workflow ---------------------------------------------------------------------------------


def test_each_probe_of_the_seed_7_world_has_a_key_a_proof_trace_and_a_contest_set(
    tmp_path: Path,
) -> None:
    plan = plan_world(RoundSpec(SLICE_ONE), CATALOGUE, PACK, VOCABULARY, "dev", 7)
    digests = [render(plan, CATALOGUE, BANK, STATED).write(plan, tmp_path / n) for n in "ab"]
    assert digests[0] == digests[1]
    keys = json.loads((tmp_path / "a" / "keys.json").read_text(encoding="utf-8"))
    probes = json.loads((tmp_path / "a" / "probes.json").read_text(encoding="utf-8"))
    world = plan.world()

    assert {(k["thread"], k["form"]) for k in keys} == {(p["thread"], p["form"]) for p in probes}
    assert len(keys) == len(probes) == 56
    for k in keys:
        assert set(k["proof"]) <= set(k["contest"]) <= set(world.artefacts)
        dates = [world.artefacts[a].at.isoformat() for a in k["contest"]]
        assert dates == sorted(dates)  # session order, which the gold-evidence arm reads
    manifest = json.loads((tmp_path / "a" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rendering"]["keys"] == {
        "count": 56,
        "digest": keys_digest(write_keys(plan, CATALOGUE)),
    }


# -- the two checks of the task -------------------------------------------------------------------


def test_the_contest_set_of_a_replaced_value_holds_the_document_that_carries_it() -> None:
    plan, thread, key = keyed("currency.reversal", "state")

    assert key.traps == (thread.roles["old"],)
    assert key.proof == tuple(carriers(plan, thread, "second"))
    assert carriers(plan, thread, "first") <= set(key.contest)
    assert not carriers(plan, thread, "first") & set(key.proof)


def test_the_contest_set_of_an_exception_holds_the_default_that_it_overrides() -> None:
    plan, thread, key = keyed("scope.exception", "state")

    # The default is no ended statement. The trap is its value, which a rule holds and no statement.
    assert key.traps == (thread.roles["usual"],)
    assert carriers(plan, thread, "general") <= set(key.contest)
    assert not carriers(plan, thread, "general") & set(key.proof)


@pytest.mark.parametrize(
    ("entry", "rule"),
    [
        ("provenance.primary-over-secondary", "precedence"),  # a rule says which holder governs
        ("scope.exception", "carve"),  # a rule carves an exception to a default
    ],
)
def test_the_proof_trace_of_a_key_on_a_stated_norm_holds_the_document_that_states_it(
    entry: str, rule: str
) -> None:
    plan, thread, key = keyed(entry, "state")

    assert {n for n in CATALOGUE.entries[entry].norms if CATALOGUE.norms[n] == "stated"}
    assert carriers(plan, thread, rule) <= set(key.proof)


def test_a_key_that_names_a_stated_norm_no_document_states_is_refused() -> None:
    entry = CATALOGUE.entries["currency.reversal"]
    stated = dataclasses.replace(entry, norms=("stated-rule",))
    catalogue = dataclasses.replace(CATALOGUE, entries={**CATALOGUE.entries, entry.id: stated})

    assert refusal(plan_of("currency.reversal"), catalogue) == {"no-norm-document"}


# -- what a proof trace and a contest set hold ----------------------------------------------------


def test_the_proof_trace_of_a_corrected_value_holds_the_correction_and_the_note_it_corrects() -> (
    None
):
    plan, thread, key = keyed("correction.user-correction", "state")
    correction = next(s["id"] for s in thread.steps if s["op"] == "correct")
    announcing = {a.id for a in plan.world().artefacts.values() if a.announces == correction}

    assert announcing <= set(key.proof)  # the correction states the value; the note alone is stale
    assert carriers(plan, thread, "entry") <= set(key.proof)


def test_the_contest_set_of_a_plan_whose_basis_was_replaced_holds_the_replaced_basis() -> None:
    plan, thread, key = keyed("propagation.plan-invalidated", "state")

    # The key names no value of the replaced basis, so no trap points at it: the statement that the
    # norms defeat puts it in the contest set. Its support link also needs both original endpoints.
    assert key.value is False
    assert carriers(plan, thread, "basis") <= set(key.contest)
    assert carriers(plan, thread, "basis") <= set(key.proof)


def test_a_twin_has_no_stale_state_to_presuppose() -> None:
    plan = plan_of("currency.value-update", twins=1.0)
    (thread,) = plan.threads
    key = next(k for k in write_keys(plan, CATALOGUE) if k.form == "premise")

    assert thread.variant == MUTATION
    assert key.premise is None and key.traps == ()


# -- a thread's keys draw on its own items only ---------------------------------------------------


def shared_value_plan() -> tuple[Plan, Any]:
    """The first plan of two reversal threads whose values overlap."""
    for seed in range(60):
        plan = plan_of("currency.reversal", "currency.reversal", seed=seed)
        a, b = plan.threads
        shared = {a.roles["old"], a.roles["new"]} & {b.roles["old"], b.roles["new"]}
        if shared:
            return plan, shared.pop()
    pytest.fail("no seed of the pack gives two reversal threads a shared value")


def test_a_value_that_two_threads_share_draws_no_item_of_the_other_thread_into_a_key() -> None:
    plan, shared = shared_value_plan()
    a, b = plan.threads
    keys = write_keys(plan, CATALOGUE)

    # The shared world holds the statements of both threads for the shared value, so reading a key
    # there would draw the other thread in.
    world = plan.world()
    last = max(a.slots["probe"], b.slots["probe"])
    bearing = {s.id.rsplit(".", 1)[0] for s in Oracle(world).bearers(shared, last)}
    assert bearing == {a.id, b.id}
    for thread in (a, b):
        mine = [k for k in keys if k.thread == thread.id]
        assert mine
        for key in mine:
            assert all(x.startswith(f"{thread.id}.") for x in key.contest), key
            assert {x for x in key.proof} <= set(key.contest)


# -- one failing case for each refusal ------------------------------------------------------------


def test_a_reading_that_is_not_built_is_refused_by_name() -> None:
    plan = plan_of("quantity.missing-term")  # its key reads `quantity_calc`, which no slice builds

    assert refusal(plan) == {"unbuilt-reading"}


def test_a_premise_that_reads_two_stale_states_is_refused() -> None:
    plan = plan_of("currency.reversal")
    (thread,) = plan.threads
    world = plan.pair(thread)[0]
    domain = plan.backbone.attributes[thread.roles["item"]].schema.domain.values()
    middle = next(v for v in domain if v not in (thread.roles["old"], thread.roles["new"]))
    world.op_state(
        None,
        by=thread.roles["owner"],
        about=thread.roles["item"],
        value=middle,
        kind="decision",
        at=thread.slots["before"] + timedelta(days=1),
    )
    oracle = Oracle(world, plan.validity())

    with pytest.raises(KeyRefused) as refused:
        write_key(oracle, thread, CATALOGUE, "premise")
    assert [r.code for r in refused.value.refusals] == ["premise-not-one"]


def test_a_value_that_two_holders_leave_open_is_refused_by_name() -> None:
    plan = plan_of("currency.reversal")
    (thread,) = plan.threads
    world = plan.pair(thread)[0]
    other = next(h for h in world.holders() if h not in thread.roles.values())
    world.op_state(
        None,
        by=other,
        about=thread.roles["item"],
        value=thread.roles["old"],
        kind="decision",
        at=thread.slots["change"],
    )

    with pytest.raises(KeyRefused) as refused:
        write_key(Oracle(world, plan.validity()), thread, CATALOGUE, "state")
    assert [r.code for r in refused.value.refusals] == ["not-unique"]
    assert thread.id in str(refused.value)


def test_graph_evidence_keeps_ended_links_both_endpoints_and_excludes_future_sources() -> None:
    """G must see why a withdrawn basis once supported a conclusion, without future evidence."""
    from benchmarks.membench.world.proof import Prover, artefacts

    plan, thread, _ = keyed("propagation.plan-invalidated", "state")
    world = plan.thread_world(thread, thread.variant)
    basis, conclusion = (thread.step_ids[s] for s in ("basis", "scheme"))
    at = thread.slots["probe"]
    future = "later-record"
    world.op_state(
        future,
        by=thread.roles["owner"],
        about=world.statements[basis].about,
        value="later",
        kind="fact",
        at=at + timedelta(days=7),
    )
    world.add_artefact(f"{future}.doc", at + timedelta(days=7), (future,), "note")
    world.op_support(None, **{"from": future, "to": conclusion})
    ref = core.EdgeRef(future, conclusion, None)
    world.add_artefact("later-link", at + timedelta(days=7), kind="register", relations=(ref,))
    refs = Prover(Oracle(world, plan.validity())).expand((conclusion,), at=at)
    docs = artefacts(world, refs, at=at)
    assert {f"{basis}.doc", f"{conclusion}.doc"} <= set(docs)
    relation_docs = [world.artefacts[d] for d in docs if world.artefacts[d].relations]
    assert any(core.EdgeRef(basis, conclusion, None) in a.relations for a in relation_docs)
    assert f"{future}.doc" not in docs and "later-link" not in docs
    assert all(world.artefacts[d].at <= at for d in docs)


def test_scope_evidence_includes_the_requested_unmentioned_sibling_and_ancestor_chain() -> None:
    """G needs the requested directory branch even when only its sibling occurs in statements."""
    plan, thread, key = keyed("scope.general-rule-applies", "state")
    from benchmarks.membench.world.keys import key_spec

    world = plan.thread_world(thread, thread.variant)
    requested = thread.resolve(key_spec(CATALOGUE, thread, "state")["value"]["where"])
    assert requested not in {s.scope for s in world.statements.values()}
    needed = set()
    child = requested
    while world.contexts[child].parent is not None:
        needed.add(core.ContextParentRef(child))
        child = world.contexts[child].parent
    selected = {ref for a in key.contest for ref in world.artefacts[a].relations}
    assert needed and needed <= selected


def test_a_past_reading_does_not_reopen_its_trace_at_the_later_probe_date() -> None:
    """A later replacement must not leak into G's evidence for a reading of an earlier date."""
    plan, thread, _ = keyed("currency.reversal", "state")
    recipe = CATALOGUE.recipes[thread.recipe]
    state = recipe["keys"]["state"]
    historical = {**state, "value": {**state["value"], "when": "before"}, "traps": []}
    catalogue = dataclasses.replace(
        CATALOGUE,
        recipes={**CATALOGUE.recipes, thread.recipe: {**recipe, "keys": {"state": historical}}},
    )
    world = plan.thread_world(thread, thread.variant)
    key = write_key(Oracle(world, plan.validity()), thread, catalogue, "state")
    assert key.value == thread.roles["old"]
    assert carriers(plan, thread, "first") <= set(key.contest)
    assert not carriers(plan, thread, "second") & set(key.contest)
    assert all(world.artefacts[a].at <= thread.slots["before"] for a in key.contest)
