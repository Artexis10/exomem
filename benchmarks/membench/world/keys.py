"""The key writer: each probe's key, proof trace and contest set, read from the oracle.

A key is read in one thread's own world, the backbone and that thread alone (``Plan.pair``), so a
value that two threads share never draws a statement of another thread into a key, a trap, a proof
trace or a contest set.

* **Key.** The value, the traps, the premise and the facts that the recipe's key parts read, in the
  world that the plan chose for the thread. A trap that equals the value is no trap.
* **Proof trace.** The documents that carry what the value and the facts rest on: the statements, the
  operations that changed them, and each rule that the reading applied. A stated norm lives in the
  document of a rule, so the proof trace holds the document that states it.
* **Contest set.** The proof trace, the documents that hold a trap or the premise, and the documents
  of statements that ended and of what ended them. The gold-evidence arm reads it.

Every list of documents is in the order of the calendar. No key reads rendered text, so a key and
its proof trace are the same at every implicitness level.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from . import manifest
from .core import World
from .loader import Catalogue, Refusal
from .mechanisms import Oracle
from .planner import MUTATION, Plan, Thread
from .proof import Basis, NoBasis, Prover, artefacts
from .record import NotUnique, distinct

# The operation of a document that states a norm besides a rule: who may see a fact.
_RESTRICT = "restrict"
# The basis of a norm that a document of the world states, as norms.yaml declares it.
_STATED = "stated"


class KeyRefused(Exception):
    """A key cannot be written; every refusal is in ``refusals``, each naming its thread and form."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


@dataclass(frozen=True)
class Fact:
    """A further reading that the judge needs, with the reading's name and subject."""

    reading: str
    of: Any
    value: Any


@dataclass(frozen=True)
class Key:
    thread: str
    entry: str
    form: str
    at: date
    value: Any
    traps: tuple[Any, ...]
    premise: Any  # the stale state that a premise probe presupposes, or None when there is none
    facts: tuple[Fact, ...]
    proof: tuple[str, ...]  # artefact ids
    contest: tuple[str, ...]  # artefact ids, the proof trace included


def keys_record(keys: tuple[Key, ...]) -> list[dict[str, Any]]:
    return [asdict(k) for k in keys]


def keys_digest(keys: tuple[Key, ...]) -> str:
    return manifest.digest(keys_record(keys))


def write_keys(plan: Plan, catalogue: Catalogue) -> tuple[Key, ...]:
    """Every key of every probe of the plan, each in the world that the plan chose for its thread.

    Raises ``KeyRefused`` with every refusal: a reading that gives two answers, a reading that is not
    built or has no basis, a premise that is not one state, and a stated norm that no proof document
    states."""
    keys: list[Key] = []
    refusals: list[Refusal] = []
    for thread in plan.threads:
        planted, mutation = plan.pair(thread)
        oracle = Oracle(mutation if thread.variant == MUTATION else planted, plan.validity())
        entry = catalogue.entries[thread.entry]
        for form in entry.probes:
            try:
                keys.append(
                    write_key(oracle, thread, catalogue, form, twin=thread.variant == MUTATION)
                )
            except KeyRefused as refused:
                refusals += refused.refusals
    if refusals:
        raise KeyRefused(refusals)
    return tuple(keys)


def write_key(
    oracle: Oracle, thread: Thread, catalogue: Catalogue, form: str, *, twin: bool = False
) -> Key:
    """The key of one probe form of a thread, read from ``oracle``, which holds the thread's world.

    ``twin`` says that the thread is planted in the mutation world: a role trap that a statement
    which asserts and stands carries is then no error (the asserted-content rule of the recipes).
    A twin entry keys the recipe's `twin` part."""
    entry = catalogue.entries[thread.entry]
    recipe = catalogue.recipes[thread.recipe]
    spec = (recipe["twin"]["keys"] if entry.twin_of else recipe["keys"])[form]
    at = thread.slots[spec["at"]]
    world = oracle.record.world
    prover = Prover(oracle)
    where = f"probe {thread.id} {form}"

    def refuse(code: str, detail: str) -> KeyRefused:
        return KeyRefused([Refusal(code, where, f"{thread.entry}: {detail}")])

    def arguments(part: Mapping[str, Any]) -> dict[str, Any]:
        return {
            ("for_" if name == "for" else name): thread.resolve(ref)
            for name, ref in part.items()
            if name not in ("read", "of", "when")
        }

    def read(part: Mapping[str, Any], *, rests: bool = False) -> tuple[Any, Basis | None]:
        """A part's answer and, if asked, what the answer rests on. A role is its own answer."""
        if "role" in part:
            return thread.roles[part["role"]], None
        reading, subject = part["read"], thread.resolve(part["of"])
        when = thread.slots[part["when"]] if "when" in part else at
        if reading not in oracle.readings():
            raise refuse("unbuilt-reading", f"`{reading}` is not built")
        try:
            answer = oracle.read(reading, of=subject, at=when, **arguments(part))
            basis = prover.basis(reading, of=subject, at=when, **arguments(part)) if rests else None
        except NotUnique as unique:
            raise refuse("not-unique", unique.detail) from unique
        except NoBasis as none:
            raise refuse("no-basis", str(none)) from none
        return answer, basis

    value, basis = read(spec["value"], rests=True)
    bases = [basis] if basis else []
    facts = []
    for part in spec.get("facts", []):
        answer, fact_basis = read(part, rests=True)
        facts.append(Fact(part["read"], thread.resolve(part["of"]), answer))
        bases += [fact_basis] if fact_basis else []

    traps: list[Any] = []
    for trap in spec.get("traps", []):
        if "role" in trap:
            role = thread.roles[trap["role"]]
            traps += [role] if oracle.role_trap(role, at, planted_in_mutation=twin) else []
            continue
        answer, _ = read(trap)
        if answer == value:  # a trap that equals the value is no trap
            continue
        traps += (
            list(answer) if isinstance(answer, tuple | list) else [] if answer is None else [answer]
        )
    traps = [t for t in distinct(traps) if t != value]

    stale: tuple[Any, ...] = ()
    if "premise" in spec:
        answer, _ = read(spec["premise"])
        stale = (
            tuple(answer)
            if isinstance(answer, tuple | list)
            else ()
            if answer is None
            else (answer,)
        )
        if len(stale) > 1:
            raise refuse("premise-not-one", f"the premise reads {len(stale)} stale states: {stale}")

    proof_refs = [ref for b in bases for ref in b.proof]
    ended = [ref for b in bases for ref in b.aside]
    carriers = [ref for held in (*traps, *stale) for ref in _carriers(oracle, held, at)]
    proof = artefacts(world, proof_refs)
    if _rests_on_stated_norm(entry.norms, catalogue) and not any(
        _states_a_norm(world, a) for a in proof
    ):
        raise refuse(
            "no-norm-document",
            "the entry names a stated norm and no document of the proof trace states one",
        )
    contest = artefacts(world, [*proof_refs, *ended, *carriers])
    return Key(
        thread.id,
        thread.entry,
        form,
        at,
        value,
        tuple(traps),
        stale[0] if stale else None,
        tuple(facts),
        proof,
        contest,
    )


def _carriers(oracle: Oracle, trap: Any, at: date) -> list[str]:
    """What holds the trap by the probe's date: the statements and corrections that give it as content,
    and the rules that give it as their value. A document holds a rule's value, though the carry rule
    of a role trap does not count it."""
    world = oracle.record.world
    found = [s.id for s in oracle.bearers(trap, at)]
    found += [
        s.id for s in world.statements.values() if s.at <= at and s.rule and s.rule.value == trap
    ]
    return found + [
        oracle.record.op_id(c) for c in world.corrections if c.at <= at and c.value == trap
    ]


def _rests_on_stated_norm(norms: tuple[str, ...], catalogue: Catalogue) -> bool:
    return any(catalogue.norms.get(norm) == _STATED for norm in norms)


def _states_a_norm(world: World, artefact_id: str) -> bool:
    """Whether a document states a norm: it carries a rule, or announces who may see a fact."""
    artefact = world.artefacts[artefact_id]
    if artefact.announces is not None:
        return world.operations[artefact.announces][0] == _RESTRICT
    return any(world.statements[s].rule is not None for s in artefact.carries)
