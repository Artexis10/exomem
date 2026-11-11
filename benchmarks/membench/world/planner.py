"""The planner: it plants one thread for each phenomenon of a round on the entities of one world.

A thread is a recipe with its roles bound to things of the backbone and its slots placed on the
session timeline. The planner decides three things and records each in the plan:

* **Binding.** Each role of a recipe binds to a backbone thing that meets the role's constraints. A
  role binds to one thing that no other role of its kind in the thread holds. A recipe that cannot
  be bound after ``ATTEMPTS`` draws is dropped, and the drop names the role.
* **Reservation.** An attribute, an entity or an event that a thread binds belongs to that thread
  alone. No other thread binds it, so no other thread changes what a thread's key reads.
* **Placement.** Each slot takes a week. A recipe that binds an attribute of a volatility class with a
  validity window places its probe within one session of the moment before it and every other
  moment beyond the window, so that an observation is current at the probe in one world only.

A thread has two worlds. ``Plan.pair`` builds them: the backbone plus that thread alone, planted and
left out. ``Plan.world`` builds the shared world that a renderer reads, with each thread in the form
that the plan chose for it. The oracle's checks read a thread's own two worlds, because its carry
rule reads every statement of the world that it is given.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random
from typing import Any

from membench.clock import week_date
from membench.ids import child_seed

from . import manifest
from .backbone import Backbone, build_backbone
from .core import Vocabulary, World
from .loader import SLOT_ARGUMENTS, WORD_ARGUMENTS, Catalogue, Refusal
from .names import NamePool
from .pack import Domain, Pack

# Closed values that the planner implements: the nine role kinds and the constraints that it reads,
# the step world that applies in either world, and the volatility class of an attribute that no role
# constrains (the class that holds a value until a recorded change).
HOLDER, USER, ENTITY, ATTRIBUTE, VALUE = "holder", "user", "entity", "attribute", "value"
NAME, CONTEXT, EVENT, AUDIENCE = "name", "context", "event", "audience"
PLANTED, MUTATION = "planted", "mutation"
DURABLE = "durable"
# The operations that make a statement or a document, and so need an id of their own.
_MAKES = ("state", "rule", "copy")
# The operation that gives an entity a name, and the argument that holds the name.
_NAMES, _NAME_ARGUMENT = "name", "name"

# Draws of a whole binding that the planner makes before it drops the recipe.
ATTEMPTS = 16
# Days of a week that a thread's moments may fall on: Monday to Friday.
_WORKDAYS = 5
_WEEK = 7


class PlanRefused(Exception):
    """The round names something that the planner cannot build."""

    def __init__(self, refusal: Refusal):
        self.refusal = refusal
        super().__init__(str(refusal))


@dataclass(frozen=True)
class RoundSpec:
    """What a round asks of the planner: the phenomena to plant, and the dials that this layer reads."""

    entries: tuple[str, ...]
    sessions: int = 12  # dial `history`: the weeks of the world's timeline that slots may fall in
    distance: int = (
        2  # dial `distance`: one gap between two moments of a thread is 1 to this many sessions
    )
    twins: float = 0.25  # dial `twins`: the share of threads that are planted in the mutation world


@dataclass(frozen=True)
class Drop:
    """A phenomenon that the plan could not plant, and why."""

    entry: str
    code: str
    detail: str


@dataclass(frozen=True)
class Thread:
    id: str
    entry: str
    recipe: str
    family: str
    variant: str  # the world that the shared world holds this thread in
    roles: Mapping[str, Any]
    slots: Mapping[str, date]
    steps: tuple[Mapping[str, Any], ...]  # the recipe's steps, with roles, slots and ids resolved
    reserved: tuple[str, ...]
    step_ids: Mapping[str, str] = field(default_factory=dict)  # a recipe's step id and its world id

    def resolve(self, ref: Any) -> Any:
        """What a recipe's reference stands for in the world: a role's value or a step's id."""
        return _resolve(ref, self.roles, self.step_ids)


@dataclass(frozen=True)
class Fact:
    """A stable fact: a holder states an attribute's value, which the world holds from week 0."""

    id: str
    attribute: str
    value: Any
    by: str
    at: date


def _resolve(ref: Any, roles: Mapping[str, Any], step_ids: Mapping[str, str]) -> Any:
    if isinstance(ref, list | tuple):
        return [_resolve(r, roles, step_ids) for r in ref]
    if isinstance(ref, str):
        return roles[ref] if ref in roles else step_ids.get(ref, ref)
    return ref


class _Cannot(Exception):
    """A recipe cannot be planted: ``code`` names the kind of failure and ``detail`` the cause."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


@dataclass
class Plan:
    spec: RoundSpec
    split: str
    name: str  # the world's name, which every id begins with
    seed: int
    pack: Pack
    backbone: Backbone
    vocabulary: Vocabulary
    catalogue_digest: str
    threads: tuple[Thread, ...]
    drops: tuple[Drop, ...]
    facts: tuple[Fact, ...]

    def thread(self, id_: str) -> Thread:
        return next(t for t in self.threads if t.id == id_)

    def validity(self) -> dict[str, timedelta]:
        """How long an observation of each volatility class with a window stays current."""
        return {cls: timedelta(days=days) for cls, days in self.pack.windows.items()}

    def world(self, variants: Mapping[str, str] | None = None) -> World:
        """The shared world: the backbone, every thread in its chosen form and the stable facts."""
        return self._build(self.threads, variants or {}, self.facts)

    def pair(self, thread: Thread) -> tuple[World, World]:
        """A thread's planted world and its mutation world: the backbone and that thread alone."""
        planted = self._build((thread,), {thread.id: PLANTED}, ())
        return planted, self._build((thread,), {thread.id: MUTATION}, ())

    def _build(
        self, threads: Sequence[Thread], variants: Mapping[str, str], facts: Iterable[Fact]
    ) -> World:
        world = World(self.name, self.vocabulary)
        self.backbone.declare(world)
        for thread in threads:
            variant = variants.get(thread.id, thread.variant)
            for step in thread.steps:
                self._file(world, world.apply(step, variant))
        for fact in facts:
            world.op_set(None, target=fact.attribute, value=fact.value, at=week_date(0))
            self._file(
                world,
                world.op_state(
                    fact.id,
                    by=fact.by,
                    about=fact.attribute,
                    value=fact.value,
                    kind=self.pack.fact_kind,
                    at=fact.at,
                ),
            )
        return world

    def _file(self, world: World, made: str | None) -> None:
        """Put each statement into a document of its own, in the pack's kind for its statement kind."""
        if made is not None and made in world.statements:
            statement = world.statements[made]
            kind = self.pack.artefact_kind(statement.kind)
            world.add_artefact(f"{made}.doc", statement.at, [made], kind, statement.by)

    def manifest(self) -> dict[str, Any]:
        """Everything that fixes the world's bytes, and a hash of each artefact."""
        world = self.world()
        return {
            "generator": manifest.GENERATOR_VERSION,
            "split": self.split,
            "world": self.name,
            "seed_commitment": manifest.digest(self.seed),
            "catalogue_digest": self.catalogue_digest,
            "pack": {"id": self.pack.id, "digest": self.pack.digest},
            "projection_digest": None,  # no projection has written this world yet
            "round": {
                "entries": list(self.spec.entries),
                "sessions": self.spec.sessions,
                "distance": self.spec.distance,
                "twins": self.spec.twins,
            },
            "sizes": {
                "entities": len(world.entities),
                "attributes": len(world.attributes),
                "threads": len(self.threads),
                "statements": len(world.statements),
                "artefacts": len(world.artefacts),
            },
            "threads": [
                {
                    "id": t.id,
                    "entry": t.entry,
                    "recipe": t.recipe,
                    "family": t.family,
                    "variant": t.variant,
                    "roles": dict(t.roles),
                    "slots": {name: when.isoformat() for name, when in t.slots.items()},
                    "reserved": list(t.reserved),
                }
                for t in self.threads
            ],
            "drops": [{"entry": d.entry, "code": d.code, "detail": d.detail} for d in self.drops],
            "artefacts": manifest.artefact_hashes(world),
        }


def plan_world(
    spec: RoundSpec,
    catalogue: Catalogue,
    pack: Pack,
    vocabulary: Vocabulary,
    split: str,
    master_seed: int,
    index: int = 0,
) -> Plan:
    """Plan world ``index`` of ``split`` from a master seed. The same inputs give the same plan."""
    if split not in vocabulary.splits:
        raise PlanRefused(Refusal("undeclared-value", f"split {split}", "is no declared split"))
    if not (spec.sessions >= 2 and spec.distance >= 1 and 0 <= spec.twins <= 1):
        raise PlanRefused(Refusal("invalid-value", "round", "dials are out of range"))
    for entry in spec.entries:
        if entry not in catalogue.buildable:
            lacking = ", ".join(catalogue.lacking.get(entry, ("is not in the catalogue",)))
            raise PlanRefused(Refusal("not-buildable", f"phenomenon {entry}", lacking))
    name = f"{split}-{index:02d}"
    seed = child_seed(master_seed, f"world:{split}", index)
    pool = NamePool(split, vocabulary.splits)
    backbone = build_backbone(pack, name, pool, Random(child_seed(seed, "backbone", 0)))
    planner = _Planner(spec, catalogue, pack, backbone, pool, name, seed)
    threads, drops = planner.plant()
    return Plan(
        spec,
        split,
        name,
        seed,
        pack,
        backbone,
        vocabulary,
        manifest.catalogue_digest(),
        threads,
        drops,
        planner.facts(),
    )


class _Planner:
    def __init__(
        self,
        spec: RoundSpec,
        catalogue: Catalogue,
        pack: Pack,
        backbone: Backbone,
        pool: NamePool,
        name: str,
        seed: int,
    ):
        self.spec, self.catalogue, self.pack = spec, catalogue, pack
        self.backbone, self.pool, self.name, self.seed = backbone, pool, name, seed
        self.reserved: dict[str, str] = {}  # a reserved thing, and the thread that holds it
        self.threads: list[Thread] = []

    # -- planting ------------------------------------------------------------------------------

    def plant(self) -> tuple[tuple[Thread, ...], tuple[Drop, ...]]:
        drops: list[Drop] = []
        for n, entry_id in enumerate(self.spec.entries):
            entry = self.catalogue.entries[entry_id]
            recipe_id = entry.twin_of or entry_id
            recipe = self.catalogue.recipes[recipe_id]
            thread_id = f"{self.name}.t{n:02d}"
            rng = Random(child_seed(self.seed, "thread", n))
            try:
                roles = self.bind_with_attempts(recipe, rng)
                slots = self.place(recipe, roles, rng)
            except _Cannot as cannot:
                drops.append(Drop(entry_id, cannot.code, cannot.detail))
                continue
            twin_world = (recipe.get("twin") or {}).get("world")
            if entry.twin_of is not None:
                variant = MUTATION if twin_world == MUTATION else PLANTED
            else:
                variant = MUTATION if rng.random() < self.spec.twins else PLANTED
            reserved = self.reserve(thread_id, recipe, roles)
            steps, step_ids = self.resolve_steps(thread_id, recipe, roles, slots)
            self.threads.append(
                Thread(
                    thread_id,
                    entry_id,
                    recipe_id,
                    entry.family,
                    variant,
                    roles,
                    slots,
                    steps,
                    reserved,
                    step_ids,
                )
            )
        return tuple(self.threads), tuple(drops)

    def reserve(
        self, thread_id: str, recipe: Mapping[str, Any], roles: Mapping[str, Any]
    ) -> tuple[str, ...]:
        held = sorted(
            roles[role]
            for role, need in recipe["needs"].items()
            if need["is"] in (ATTRIBUTE, ENTITY, EVENT)
        )
        for ref in held:
            self.reserved[ref] = thread_id
        return tuple(held)

    # -- binding -------------------------------------------------------------------------------

    def bind_with_attempts(self, recipe: Mapping[str, Any], rng: Random) -> dict[str, Any]:
        failure = _Cannot("unbound-role", "no attempt was made")
        for _ in range(ATTEMPTS):
            try:
                return self.bind(recipe, rng)
            except _Cannot as cannot:
                failure = cannot
        raise failure

    def bind(self, recipe: Mapping[str, Any], rng: Random) -> dict[str, Any]:
        needs = recipe["needs"]
        introduced = {
            step[_NAME_ARGUMENT]
            for step in recipe["steps"]
            if step.get("op") == _NAMES and _NAME_ARGUMENT in step
        }
        bound: dict[str, Any] = {}
        for role in _dependency_order(needs):
            bound[role] = self.pick(role, needs, bound, introduced, rng)
        return bound

    def pick(
        self,
        role: str,
        needs: Mapping[str, Any],
        bound: Mapping[str, Any],
        introduced: set[str],
        rng: Random,
    ) -> Any:
        need = needs[role]
        kind = need["is"]
        taken = {bound[r] for r in bound if needs[r]["is"] == kind}
        unavailable = taken | self.reserved.keys()
        if kind == USER:
            return self.backbone.user
        if kind == NAME:
            return self.pick_name(role, need, bound, introduced, rng)
        if kind == VALUE:
            domain = self.domain_of(bound.get(need.get("of")))
            avoid = {bound[r] for r in bound if needs[r]["is"] == VALUE}
            if need.get("differs_from") in bound:
                avoid.add(bound[need["differs_from"]])
            options = [v for v in (domain.values() if domain else ()) if v not in avoid]
        elif kind == HOLDER:
            options = [h for h in self.backbone.holders() if h not in taken]
            outside = bound.get(need.get("outside"))
            if outside in self.backbone.audiences:
                options = [h for h in options if h not in self.backbone.audiences[outside]]
        elif kind == ENTITY:
            options = [e for e in self.backbone.things() if e not in unavailable]
        elif kind == EVENT:
            options = [e for e in sorted(self.backbone.events) if e not in unavailable]
        elif kind == ATTRIBUTE:
            volatility = need.get("volatility", DURABLE)
            owner = bound.get(need.get("of"))
            options = [
                a
                for a in sorted(self.backbone.attributes)
                if self.backbone.attributes[a].schema.volatility == volatility
                and (owner is None or self.backbone.attributes[a].entity == owner)
                and a not in unavailable
            ]
        elif kind == CONTEXT:
            options = self.context_options(role, need, needs, bound, taken)
        elif kind == AUDIENCE:
            options = [a for a in sorted(self.backbone.audiences) if a not in taken]
        else:
            raise _Cannot("unbound-role", f"role {role}: no rule binds a role of kind `{kind}`")
        if not options:
            raise _Cannot("unbound-role", f"role {role}: no free {kind} meets its constraints")
        return options[rng.randrange(len(options))]

    def domain_of(self, ref: Any) -> Domain | None:
        if ref in self.backbone.attributes:
            return self.backbone.attributes[ref].schema.domain
        if ref in self.backbone.events:
            return self.backbone.events[ref].type.domain
        return None

    def pick_name(
        self,
        role: str,
        need: Mapping[str, Any],
        bound: Mapping[str, Any],
        introduced: set[str],
        rng: Random,
    ) -> str:
        entity = self.backbone.entities.get(bound.get(need.get("of")))
        if entity is None or entity.type is None:
            raise _Cannot(
                "unbound-role", f"role {role}: a name needs an entity that the pack types"
            )
        if role not in introduced:
            return entity.name  # no step gives the name, so it is one that the entity already holds
        space = next(t.names for t in self.pack.entity_types if t.id == entity.type)
        return self.pool.take(space, rng)

    def context_options(
        self,
        role: str,
        need: Mapping[str, Any],
        needs: Mapping[str, Any],
        bound: Mapping[str, Any],
        taken: set[Any],
    ) -> list[str]:
        if "within" in need:
            options = self.backbone.children_of(bound[need["within"]])
        else:
            inside = sum(1 for n in needs.values() if n.get("within") == role)
            options = [
                c
                for c in sorted(self.backbone.contexts)
                if len(self.backbone.children_of(c)) >= inside
            ]
        outside = bound.get(need.get("outside"))
        return [c for c in options if c not in taken and c != outside]

    # -- placing -------------------------------------------------------------------------------

    def place(
        self, recipe: Mapping[str, Any], roles: Mapping[str, Any], rng: Random
    ) -> dict[str, date]:
        slots = list(recipe["slots"])
        probed = {k["at"] for k in recipe["keys"].values()}
        probed |= {k["at"] for k in ((recipe.get("twin") or {}).get("keys") or {}).values()}
        windows = [
            self.pack.windows[self.backbone.attributes[roles[r]].schema.volatility]
            for r, need in recipe["needs"].items()
            if need["is"] == ATTRIBUTE
            and self.backbone.attributes[roles[r]].schema.volatility in self.pack.windows
        ]
        room = self.spec.sessions - 1
        if windows:
            beyond = math.ceil(min(windows) / _WEEK) + 1
            gaps = [1 if slot in probed else beyond for slot in slots[1:]]
        else:
            gaps = [1 + rng.randrange(self.spec.distance) for _ in slots[1:]]
            if sum(gaps) > room:
                gaps = [1] * len(gaps)
        if sum(gaps) > room:
            raise _Cannot(
                "slots-do-not-fit",
                f"the {len(slots)} moments of the recipe do not fit in {self.spec.sessions} sessions",
            )
        week = 1 + rng.randrange(self.spec.sessions - sum(gaps))
        day = rng.randrange(_WORKDAYS)
        placed = {}
        for slot, gap in zip(slots, [0, *gaps], strict=True):
            week += gap
            placed[slot] = week_date(week, day)
        return placed

    # -- resolving -----------------------------------------------------------------------------

    def resolve_steps(
        self,
        thread_id: str,
        recipe: Mapping[str, Any],
        roles: Mapping[str, Any],
        slots: Mapping[str, date],
    ) -> tuple[tuple[Mapping[str, Any], ...], dict[str, str]]:
        step_ids = {s["id"]: f"{thread_id}.{s['id']}" for s in recipe["steps"] if "id" in s}
        steps = []
        for index, step in enumerate(recipe["steps"]):
            resolved: dict[str, Any] = {}
            for arg, value in step.items():
                if arg in ("op", "world") or arg in WORD_ARGUMENTS:
                    resolved[arg] = value
                elif arg == "id":
                    resolved[arg] = step_ids[value]
                elif arg in SLOT_ARGUMENTS:
                    resolved[arg] = slots[value]
                else:
                    resolved[arg] = _resolve(value, roles, step_ids)
            if "id" not in step and step["op"] in _MAKES:
                resolved["id"] = f"{thread_id}.s{index}"
            steps.append(resolved)
        return tuple(steps), step_ids

    # -- the backbone's own statements -----------------------------------------------------------

    def facts(self) -> tuple[Fact, ...]:
        """A stable fact for a share of the attributes that no thread reserved."""
        rng = Random(child_seed(self.seed, "facts", 0))
        holders = self.backbone.holders()
        facts = []
        for attribute in sorted(self.backbone.attributes):
            if attribute in self.reserved or rng.random() >= self.pack.fact_share:
                continue
            values = self.backbone.attributes[attribute].schema.domain.values()
            week = 1 + rng.randrange(self.spec.sessions)
            facts.append(
                Fact(
                    f"{attribute}.fact",
                    attribute,
                    values[rng.randrange(len(values))],
                    holders[rng.randrange(len(holders))],
                    week_date(week, rng.randrange(_WORKDAYS)),
                )
            )
        return tuple(facts)


def _dependency_order(needs: Mapping[str, Any]) -> list[str]:
    """The roles of a recipe, each after the roles that its constraints name."""
    pending, ordered = list(needs), []
    while pending:
        ready = next(
            (
                r
                for r in pending
                if all(
                    ref not in needs or ref in ordered
                    for ref in (
                        needs[r].get("of"),
                        needs[r].get("within"),
                        needs[r].get("differs_from"),
                        needs[r].get("outside"),
                    )
                )
            ),
            None,
        )
        if ready is None:
            raise _Cannot(
                "unbound-role", f"role {pending[0]}: its constraints name each other in a cycle"
            )
        ordered.append(ready)
        pending.remove(ready)
    return ordered
