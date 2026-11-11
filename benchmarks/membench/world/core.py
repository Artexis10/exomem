"""The world core: the first two layers of a synthetic world, and the operations that build them.

A ``World`` records what a recipe's steps do. Layer one is the history: entities, attributes with a
value over time, and events. Layer two is the evidence: statements that holders made, the edges
between them, and the artefacts that carry them. The third layer, the warranted state, is the
oracle's work and is not here.

Design rules for this module:

* An operation records. It refuses a step that is not shaped like its declaration (an undeclared
  operation or argument, a missing required argument, an id used twice), and nothing else. It does
  not check that a reference resolves or that times are in order: ``validator.validate`` does,
  over the finished record, so a malformed world can exist and the validator can name its fault.
* The operations are the ``op_*`` methods. ``implemented()`` lists them, so the set that code
  implements is stated once, and ``attributes.yaml`` is the set that the catalogue declares.
* A time is a ``datetime.date``. The planner turns a recipe's slots into dates with membench's
  clock. The world's own order of events inside one date is the order of the calls (``seq``).
* Code reads closed values only: a statement kind's commitment, a volatility class, a rule shape.
  It never reads a statement kind, a hedge or any other open word by name.
"""

from __future__ import annotations

import inspect
import itertools
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from membench.ids import stable_id

from .loader import CATALOGUE_DIR

# Closed values that this module implements. `BOTH` is the step world that applies in either world;
# `PERMANENT` is the volatility class whose value cannot change; `ASSERTED` and `REPORTED` are the
# commitments that put a content forward as a fact or as another holder's words; `CHANCE` is the
# rule shape that records the chance of an outcome.
BOTH = "both"
PERMANENT = "permanent"
ASSERTED = "asserted"
REPORTED = "reported"
CHANCE = "chance"

# A step's own fields, which no operation declares as an argument.
_STEP_FIELDS = ("op", "id", "world")


class StepRefused(Exception):
    """A step is not shaped like its operation's declaration. ``code`` and ``item`` name why."""

    def __init__(self, code: str, item: str, detail: str):
        self.code, self.item, self.detail = code, item, detail
        super().__init__(f"{code}: {item}: {detail}")


@dataclass(frozen=True)
class Vocabulary:
    """What the world reads from the catalogue: the closed sets that it implements."""

    operations: Mapping[str, tuple[str, ...]]
    commitments: Mapping[str, str]  # each statement kind, with its commitment
    hedges: frozenset[str]
    bases: frozenset[str]
    defeat_modes: frozenset[str]
    rule_shapes: frozenset[str]
    volatility_classes: frozenset[str]
    splits: frozenset[str] = frozenset()
    name_spaces: frozenset[str] = frozenset()
    value_generators: frozenset[str] = frozenset()
    probe_forms: frozenset[str] = frozenset()
    levels: tuple[str, ...] = ()  # the implicitness levels, in the order that the catalogue lists them

    @classmethod
    def from_raw(
        cls, attributes: Mapping[str, Any], kinds: Mapping[str, Any], hedges: Mapping[str, Any]
    ) -> Vocabulary:
        return cls(
            operations={
                name: tuple(spec["args"]) for name, spec in attributes["world_operations"].items()
            },
            commitments={name: spec["commitment"] for name, spec in kinds.items()},
            hedges=frozenset(hedges),
            bases=frozenset(attributes["statement_bases"]),
            defeat_modes=frozenset(attributes["defeat_modes"]),
            rule_shapes=frozenset(attributes["rule_shapes"]),
            volatility_classes=frozenset(attributes["volatility_classes"]),
            splits=frozenset(attributes.get("splits", ())),
            name_spaces=frozenset(attributes.get("name_spaces", ())),
            value_generators=frozenset(attributes.get("value_generators", ())),
            probe_forms=frozenset(attributes.get("probe_forms", ())),
            levels=tuple(attributes.get("implicitness_levels", ())),
        )

    @classmethod
    def load(cls, directory: Path = CATALOGUE_DIR) -> Vocabulary:
        def read(name: str) -> Any:
            return yaml.safe_load((directory / name).read_text(encoding="utf-8"))

        return cls.from_raw(
            read("attributes.yaml"),
            read("statement-kinds.yaml")["statement_kinds"],
            read("hedges.yaml")["hedges"],
        )


# -- the records ---------------------------------------------------------------------------------
# Every record is frozen. A change to the world is a new record, never an edit of an old one.


@dataclass(frozen=True)
class Entity:
    id: str
    name: str  # the name that the entity holds from `start`
    start: date
    holder: bool  # whether the entity makes statements: a person, a team, a system, an instrument


@dataclass(frozen=True)
class Attribute:
    id: str
    entity: str
    volatility: str


@dataclass(frozen=True)
class Event:
    id: str


@dataclass(frozen=True)
class Context:
    id: str
    parent: str | None  # the wider context in the scope lattice


@dataclass(frozen=True)
class Audience:
    id: str
    holders: frozenset[str]


@dataclass(frozen=True)
class Assignment:
    """`set` or `end`: an attribute takes a value from a date, holds nothing from a date, or an
    event or an entity is placed or ends at a date. ``value`` is ``None`` for an `end`."""

    target: str
    at: date
    value: Any
    scope: str | None
    ends: bool
    seq: int


@dataclass(frozen=True)
class Rule:
    shape: str
    over: str
    given: tuple[Any, ...]  # facts or values that the rule starts from; opaque to the core
    value: Any
    context: str | None


@dataclass(frozen=True)
class Statement:
    """What a holder said. A rule is a statement with a ``rule`` body and no kind."""

    id: str
    by: str
    at: date
    seq: int
    about: str | None = None
    value: Any = None
    kind: str | None = None
    hedge: str | None = None
    basis: str | None = None
    scope: str | None = None
    quoting: str | None = None
    name_used: str | None = None  # `as`: the name that the statement uses for its subject
    rule: Rule | None = None


@dataclass(frozen=True)
class Withdrawal:
    by: str
    statement: str
    at: date
    seq: int


@dataclass(frozen=True)
class Correction:
    by: str
    statement: str
    value: Any
    at: date
    seq: int


@dataclass(frozen=True)
class Edge:
    """`support` (``mode`` is ``None``) or `defeat` (``mode`` is a defeat mode)."""

    source: str
    target: str
    mode: str | None


@dataclass(frozen=True)
class Naming:
    entity: str
    name: str
    at: date
    replaces: str | None
    seq: int


@dataclass(frozen=True)
class Merge:
    entities: tuple[str, ...]  # the first entity survives; the rest are absorbed into it
    at: date
    seq: int


@dataclass(frozen=True)
class Telling:
    holder: str
    item: str  # a statement that the holder is told, or an event that the holder witnesses
    at: date
    seq: int


@dataclass(frozen=True)
class Draw:
    target: str
    chance: str  # the statement that records the chance
    at: date
    seq: int


@dataclass(frozen=True)
class Cause:
    source: str
    target: str


@dataclass(frozen=True)
class Restriction:
    target: str  # a statement or an artefact
    audience: str | None  # ``None`` records a deletion request
    by: str
    at: date
    until: date | None
    seq: int


@dataclass(frozen=True)
class Artefact:
    """One document. A copy has ``copy_of`` and carries what its source carries."""

    id: str
    kind: str | None
    by: str | None
    at: date
    carries: tuple[str, ...]
    copy_of: str | None = None
    altered: bool = False
    announces: str | None = None  # the operation that this document announces, in ``World.operations``


@dataclass(frozen=True)
class Removal:
    artefact: str  # an artefact, or a statement that exactly one artefact carries
    at: date
    seq: int


class World:
    """One world: the records of layers one and two, built by operations."""

    def __init__(self, name: str, vocabulary: Vocabulary):
        self.name = name
        self.vocabulary = vocabulary
        self.entities: dict[str, Entity] = {}
        self.attributes: dict[str, Attribute] = {}
        self.events: dict[str, Event] = {}
        self.contexts: dict[str, Context] = {}
        self.audiences: dict[str, Audience] = {}
        self.statements: dict[str, Statement] = {}
        self.artefacts: dict[str, Artefact] = {}
        self.assignments: list[Assignment] = []
        self.withdrawals: list[Withdrawal] = []
        self.corrections: list[Correction] = []
        self.edges: list[Edge] = []
        self.namings: list[Naming] = []
        self.merges: list[Merge] = []
        self.tellings: list[Telling] = []
        self.draws: list[Draw] = []
        self.causes: list[Cause] = []
        self.restrictions: list[Restriction] = []
        self.removals: list[Removal] = []
        # The operations that a document announces, by id: each with the name of its operation.
        self.operations: dict[str, tuple[str, Any]] = {}
        self._counter = itertools.count()
        # Indexes, so that a read costs one lookup and not a scan of every statement or artefact.
        self._about: dict[str, list[str]] = {}
        self._filed: dict[
            str, list[str]
        ] = {}  # statement id -> the original artefacts that carry it

    # -- the things that exist before any step runs ---------------------------------------------

    def _claim(self, id_: str, what: str) -> None:
        """Refuse an id that any record of the world already uses."""
        taken = (
            self.entities,
            self.attributes,
            self.events,
            self.contexts,
            self.audiences,
            self.statements,
            self.artefacts,
            self.operations,
        )
        if any(id_ in used for used in taken):
            raise StepRefused("duplicate-id", id_, f"{what} id is used twice in world {self.name}")

    def add_entity(self, id_: str, name: str, start: date, holder: bool = False) -> Entity:
        self._claim(id_, "entity")
        self.entities[id_] = Entity(id_, name, start, holder)
        return self.entities[id_]

    def add_attribute(self, id_: str, entity: str, volatility: str) -> Attribute:
        self._claim(id_, "attribute")
        self.attributes[id_] = Attribute(id_, entity, volatility)
        return self.attributes[id_]

    def add_event(self, id_: str) -> Event:
        self._claim(id_, "event")
        self.events[id_] = Event(id_)
        return self.events[id_]

    def add_context(self, id_: str, parent: str | None = None) -> Context:
        self._claim(id_, "context")
        self.contexts[id_] = Context(id_, parent)
        return self.contexts[id_]

    def add_audience(self, id_: str, holders: Iterable[str]) -> Audience:
        self._claim(id_, "audience")
        self.audiences[id_] = Audience(id_, frozenset(holders))
        return self.audiences[id_]

    def add_artefact(
        self,
        id_: str,
        at: date,
        carries: Iterable[str] = (),
        kind: str | None = None,
        by: str | None = None,
        announces: str | None = None,
    ) -> Artefact:
        """File statements, or announce one operation, in a document. The planner decides which."""
        self._claim(id_, "artefact")
        self.artefacts[id_] = Artefact(id_, kind, by, at, tuple(carries), announces=announces)
        for statement in self.artefacts[id_].carries:
            self._filed.setdefault(statement, []).append(id_)
        return self.artefacts[id_]

    # -- applying a step -------------------------------------------------------------------------

    def _next(self) -> int:
        return next(self._counter)

    def _fresh(self, prefix: str) -> str:
        return stable_id(prefix, self.name, str(next(self._counter)))

    def apply(self, step: Mapping[str, Any], in_world: str = BOTH) -> str | None:
        """Apply one resolved step of a recipe: roles are replaced by ids and values, slots by dates.

        A step whose ``world`` is neither ``both`` nor ``in_world`` is skipped. Returns the id of the
        statement or artefact that the step made, or ``None``.
        """
        op = step.get("op")
        handler = getattr(self, f"op_{op}", None) if isinstance(op, str) else None
        if handler is None or op not in self.vocabulary.operations:
            raise StepRefused("undeclared-operation", str(op), "no declared operation of that name")
        if step.get("world", BOTH) not in (BOTH, in_world):
            return None
        declared = self.vocabulary.operations[op]
        args = {key: value for key, value in step.items() if key not in _STEP_FIELDS}
        for key in args:
            if key not in declared:
                raise StepRefused("undeclared-argument", op, f"`{key}` is not an argument")
        for param in inspect.signature(handler).parameters.values():
            if param.kind is param.KEYWORD_ONLY and param.default is param.empty:
                if param.name not in args:
                    raise StepRefused("missing-argument", op, f"`{param.name}` is required")
        return handler(step.get("id"), **args)

    def apply_all(self, steps: Iterable[Mapping[str, Any]], in_world: str = BOTH) -> list[str]:
        """Apply a recipe's steps in order and return the ids that they made."""
        return [made for step in steps if (made := self.apply(step, in_world)) is not None]

    def _ends(self, op: str, ends: Mapping[str, Any]) -> tuple[Any, Any]:
        """The two ends of an edge, whose argument names `from` and `to` are not parameter names."""
        for key in ("from", "to"):
            if key not in ends:
                raise StepRefused("missing-argument", op, f"`{key}` is required")
        return ends["from"], ends["to"]

    def _announce(self, id_: str | None, name: str, record: Any) -> str:
        """Register an operation that a document announces, and return its id."""
        id_ = id_ or self._fresh("OP")
        self._claim(id_, "operation")
        self.operations[id_] = (name, record)
        return id_

    def _file(self, statement: Statement) -> str:
        self._claim(statement.id, "statement")
        self.statements[statement.id] = statement
        if statement.about is not None:
            self._about.setdefault(statement.about, []).append(statement.id)
        return statement.id

    # -- the operations: one `op_<name>` method for each declared operation ---------------------

    def op_set(self, id_, *, target, at, value=None, scope=None):
        self.assignments.append(Assignment(target, at, value, scope, False, self._next()))

    def op_end(self, id_, *, target, at):
        self.assignments.append(Assignment(target, at, None, None, True, self._next()))

    def op_state(
        self,
        id_,
        *,
        by,
        at,
        about=None,
        value=None,
        kind=None,
        hedge=None,
        basis=None,
        scope=None,
        quoting=None,
        **renamed,  # `as` is a keyword, so it arrives as ``renamed["as"]``
    ):
        return self._file(
            Statement(
                id_ or self._fresh("ST"),
                by,
                at,
                self._next(),
                about,
                value,
                kind,
                hedge,
                basis,
                scope,
                quoting,
                renamed.get("as"),
            )
        )

    def op_rule(self, id_, *, by, shape, over, at, given=(), value=None, context=None):
        body = Rule(
            shape,
            over,
            tuple(given) if isinstance(given, list | tuple) else (given,),
            value,
            context,
        )
        statement = Statement(id_ or self._fresh("RU"), by, at, self._next(), over, rule=body)
        return self._file(statement)

    def op_withdraw(self, id_, *, by, statement, at):
        record = Withdrawal(by, statement, at, self._next())
        self.withdrawals.append(record)
        return self._announce(id_, "withdraw", record)

    def op_correct(self, id_, *, by, statement, value, at):
        record = Correction(by, statement, value, at, self._next())
        self.corrections.append(record)
        return self._announce(id_, "correct", record)

    def op_support(self, id_, **ends):
        self.edges.append(Edge(*self._ends("support", ends), None))

    def op_defeat(self, id_, *, mode, **ends):
        self.edges.append(Edge(*self._ends("defeat", ends), mode))

    def op_name(self, id_, *, entity, name, at, replaces=None):
        record = Naming(entity, name, at, replaces, self._next())
        self.namings.append(record)
        return self._announce(id_, "name", record)

    def op_merge(self, id_, *, entities, at):
        record = Merge(tuple(entities), at, self._next())
        self.merges.append(record)
        return self._announce(id_, "merge", record)

    def op_tell(self, id_, *, holder, statement, at):
        record = Telling(holder, statement, at, self._next())
        self.tellings.append(record)
        return self._announce(id_, "tell", record)

    def op_draw(self, id_, *, target, chance, at):
        self.draws.append(Draw(target, chance, at, self._next()))

    def op_cause(self, id_, **ends):
        self.causes.append(Cause(*self._ends("cause", ends)))

    def op_restrict(self, id_, *, target, by, at, audience=None, until=None):
        record = Restriction(target, audience, by, at, until, self._next())
        self.restrictions.append(record)
        return self._announce(id_, "restrict", record)

    def op_copy(self, id_, *, artefact, at, altered=False):
        id_ = id_ or self._fresh("AR")
        self._claim(id_, "artefact")
        self.artefacts[id_] = Artefact(id_, None, None, at, (), artefact, bool(altered))
        return id_

    def op_remove(self, id_, *, artefact, at):
        self.removals.append(Removal(artefact, at, self._next()))

    # -- reading the record ----------------------------------------------------------------------

    def commitment(self, statement: Statement) -> str | None:
        """How far a statement commits its holder. A rule always asserts; an unknown kind has none."""
        if statement.rule is not None:
            return ASSERTED
        return self.vocabulary.commitments.get(statement.kind or "")

    def statements_about(self, ref: str) -> list[Statement]:
        """The statements about an attribute, an event or an entity, in the order they were made."""
        return sorted(
            (self.statements[i] for i in self._about.get(ref, ())), key=lambda s: (s.at, s.seq)
        )

    def value_at(self, attribute: str, when: date, scope: str | None = None) -> Any:
        """The hidden world's value of an attribute on a date, or ``None`` if it holds nothing."""
        held = None
        for a in sorted(
            (a for a in self.assignments if a.target == attribute and a.scope == scope),
            key=lambda a: (a.at, a.seq),
        ):
            if a.at > when:
                break
            held = None if a.ends else a.value
        return held

    def carriers(self, ref: str) -> list[Artefact]:
        """The artefacts that carry a statement, copies included."""
        originals = [self.artefacts[i] for i in self._filed.get(ref, ())]
        copies = [
            c
            for c in self.artefacts.values()
            if c.copy_of is not None and self.resolve_artefact(c.copy_of) in originals
        ]
        return originals + copies

    def resolve_artefact(self, ref: str) -> Artefact | None:
        """An artefact by id, or the one original artefact that carries a statement of that id."""
        if ref in self.artefacts:
            return self.artefacts[ref]
        filed = self._filed.get(ref, ())
        return self.artefacts[filed[0]] if len(filed) == 1 else None

    def holders(self) -> Collection[str]:
        return [e.id for e in self.entities.values() if e.holder]


def implemented() -> dict[str, tuple[str, ...]]:
    """The closed values that this module implements, for ``load_catalogue(implemented=...)``."""
    return {
        "world_operations": tuple(
            name.removeprefix("op_") for name in dir(World) if name.startswith("op_")
        )
    }
