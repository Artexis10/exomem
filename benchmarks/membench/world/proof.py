"""What a key rests on, as statements and operations, and the documents that carry them.

A reading answers from some statements and operations of the evidence layer. ``Prover.basis`` names
them for one reading. ``proof`` holds what the answer rests on, a rule that the reading applied
included, and ``aside`` holds the statements about the same subject that ended, withdrawn, replaced
or defeated, with what ended each. A ref is the id of a statement, or of an operation that a
document announces, or a canonical relation snapshot. ``artefacts`` turns refs into documents.

A reading that a key reads has a basis here. A reading with none is refused by ``NoBasis``, so no
key goes out with an empty proof trace by accident.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from .core import ContextParentRef, EdgeRef, Statement, World
from .mechanisms import Oracle
from .record import Ended, distinct, order


class NoBasis(Exception):
    """A reading is built and names no statement that its answer rests on."""


@dataclass(frozen=True)
class Basis:
    proof: tuple[str, ...] = ()
    aside: tuple[str, ...] = ()


class Prover:
    def __init__(self, oracle: Oracle):
        self.oracle, self.rec = oracle, oracle.record
        self.links: dict[str, list[EdgeRef]] = defaultdict(list)
        for edge in self.rec.world.edges:
            ref = EdgeRef(edge.source, edge.target, edge.mode)
            self.links[edge.source].append(ref)
            self.links[edge.target].append(ref)
        self.relations = {
            ref: a.id for a in self.rec.world.artefacts.values() for ref in a.relations
        }
        self._bases: dict[str, Callable[..., list[str | None]]] = {
            "corpus_closure.recorded": self._recorded,
            "holder_ledger.holder": self._holder,
            "holder_ledger.holders": self._holders,
            "identity_map.names": self._names,
            "identity_map.referent": self._referent,
            "kind_tag.hedge": self._deciding,
            "kind_tag.kind": self._deciding,
            "scope_lattice.applies": self._applies,
            "scope_lattice.value_in": self._value_in,
            "state_fold.current": self._current,
            "state_fold.history": self._folded,
            "state_fold.standing": self._folded,
            "warrant_graph.basis": self._basis,
            "warrant_graph.fallen": lambda of, at: self._lost(of, at, survives=False),
            "warrant_graph.origins": self._origins,
            "warrant_graph.sides": self._sides,
            "warrant_graph.stands": self._stands,
            "warrant_graph.surviving": lambda of, at: self._lost(of, at, survives=True),
        }

    def basis(self, reading: str, *, of: Any, at: date, **arguments: Any) -> Basis:
        if reading not in self._bases:
            raise NoBasis(f"`{reading}` has no basis yet")
        proof = tuple(distinct(r for r in self._bases[reading](of, at, **arguments) if r))
        expanded = self.expand(proof, of=of, at=at, where=arguments.get("where"))
        aside = self.expand(self._aside(of, at), of=of, at=at, where=arguments.get("where"))
        return Basis(expanded, tuple(r for r in aside if r not in expanded))

    def expand(
        self, refs: Iterable[str], *, at: date, of: Any = None, where: str | None = None
    ) -> tuple[str, ...]:
        """Close selected evidence over source edges and context ancestors, including ended links.

        The oracle's winning paths cannot stand in for the contrary source records that G must read.
        Each selected edge therefore brings both original endpoints and their endings by this date.
        """
        world = self.rec.world
        selected: dict[str, None] = {}
        pending: deque[str] = deque()
        contexts = [] if where is None else [where]

        def add(ref: str) -> None:
            record = world.statements.get(ref)
            if record is None and ref in world.operations:
                record = world.operations[ref][1]
            if record is None:
                record = world.artefacts.get(ref)
            if record is not None and record.at <= at and ref not in selected:
                selected[ref] = None
                if ref in world.statements:
                    pending.append(ref)

        for ref in refs:
            add(ref)
        subjects = of if isinstance(of, list | tuple) else (of,)
        for subject in subjects:
            if subject in world.statements:
                if world.statements[subject].at <= at:
                    pending.append(subject)
            else:
                pending.extend(s.id for s in world.statements_about(subject) if s.at <= at)
        visited: set[str] = set()
        while pending:
            ref = pending.popleft()
            if ref in visited:
                continue
            visited.add(ref)
            statement = world.statements[ref]
            contexts += [
                c
                for c in (statement.scope, statement.rule.context if statement.rule else None)
                if c is not None
            ]
            for edge in self.links.get(ref, ()):
                if any(world.statements[e].at > at for e in (edge.source, edge.target)):
                    continue
                carrier = self.relations.get(edge)
                if carrier is None or world.artefacts[carrier].at > at:
                    continue
                add(carrier)
                add(edge.source)
                add(edge.target)
            if ref in selected:
                for correction in self.rec.corrections(statement, at):
                    add(self.rec.op_id(correction))
                if end := self.rec.end(statement, at):
                    add(self._ender(statement, end))
        visited_contexts: set[str] = set()
        for child in contexts:
            while child in world.contexts and child not in visited_contexts:
                visited_contexts.add(child)
                carrier = self.relations.get(ContextParentRef(child))
                if carrier is not None:
                    add(carrier)
                child = world.contexts[child].parent
        return tuple(selected)

    # -- what ended --------------------------------------------------------------------------------

    def _dossier(self, of: Any, at: date) -> list[Statement]:
        """The statements that the world holds about a subject: about an attribute, an entity or an
        event, or about a statement, itself with the statements that support or defeat it."""
        w = self.rec.world
        if isinstance(of, list | tuple):
            return [s for item in of for s in self._dossier(item, at)]
        if of in w.statements:
            linked = [w.statements[e.source] for e in w.edges if e.target == of]
            return [w.statements[of], *(s for s in linked if s.id in w.statements)]
        return w.statements_about(of)

    def _aside(self, of: Any, at: date) -> list[str]:
        found: list[str] = []
        for s in self._dossier(of, at):
            end = s.at <= at and self.rec.end(s, at)
            if end:
                found += [s.id, self._ender(s, end)]
        return found

    def _ender(self, s: Statement, end: Any) -> str:
        """The statement that replaced or defeated ``s``, or the operation that withdrew it."""
        if end.kind is not Ended.WITHDRAWN:
            return end.ref
        withdrawal = next(
            w for w in self.rec.world.withdrawals if w.statement == s.id and order(w) == order(end)
        )
        return self.rec.op_id(withdrawal)

    # -- the readings ------------------------------------------------------------------------------

    def _recorded(self, of: str, at: date) -> list[str | None]:
        return [s.id for s in self.rec.about(of, at) if self.rec.commits(s)]

    def _holders(self, of: str, at: date) -> list[str | None]:
        rec = self.rec
        return [s.id for s in rec.about(of, at) if rec.asserts(s) and rec.intact(s, at)]

    def _holder(self, of: str, at: date, for_: Any = None) -> list[str | None]:
        return list(self.oracle.holder_ledger.trail(of, at, for_)[1])

    def _deciding(self, of: str, at: date) -> list[str | None]:
        decider = self.oracle.kind_tag.deciding(of, at)
        return [decider.id] if decider else []

    def _value_in(self, of: str, at: date, where: str | None = None) -> list[str | None]:
        decider = self.oracle.scope_lattice.deciding(of, where, at)
        return [decider.id] if decider else []

    def _applies(self, of: str, at: date, where: str | None = None) -> list[str | None]:
        about = self.rec.world.statements[of].about
        return [of, *(self._value_in(about, at, where) if about is not None else [])]

    def _current(self, of: str, at: date, where: str | None = None) -> list[str | None]:
        fold = self.oracle.state_fold.current_fold(of, at, where)
        if fold is None:
            return []
        rule = self.rec.governing_rule(of, at)
        corrections = [self.rec.op_id(c) for c in self.rec.corrections(fold.statement, at)]
        return [fold.statement.id, *corrections, rule.id if rule else None]

    def _folded(self, of: str, at: date, where: str | None = None) -> list[str | None]:
        """What a reading of the whole fold rests on: every statement that it folds, the corrections
        of each, what ended each, and the rule that governs the attribute."""
        rec, refs = self.rec, []
        for fold in self.oracle.state_fold.fold(of, at, where):
            s = fold.statement
            end = rec.end(s, at)
            refs += [s.id, *(rec.op_id(c) for c in rec.corrections(s, at))]
            refs += [self._ender(s, end)] if end else []
        rule = rec.governing_rule(of, at)
        return [*refs, rule.id if rule else None]

    def _lost(self, of: str, at: date, *, survives: bool) -> list[str | None]:
        """A statement that ended, what ended it, and the conclusions that fell or survived, with the
        statements that support the survivors."""
        graph, rec = self.oracle.warrant_graph, self.rec
        statement = rec.world.statements[of]
        end = statement.at <= at and rec.end(statement, at)
        if not end:
            return []
        kept = graph.surviving(of, at) if survives else graph.fallen(of, at)
        supporters = [x.id for c in kept for x in rec.supporters(rec.world.statements[c], at)]
        return [of, self._ender(statement, end), *kept, *supporters]

    def _basis(self, of: str, at: date) -> list[str | None]:
        rule = None if of in self.rec.world.statements else self.rec.governing_rule(of, at)
        return [*self.oracle.warrant_graph.basis(of, at), rule.id if rule else None]

    def _origins(self, of: str, at: date) -> list[str | None]:
        rec, roots = self.rec, []
        for s in rec.about(of, at):
            if rec.asserts(s) and rec.intact(s, at):
                roots += [x.id for x in rec.supporters(s, at)] or [s.id]
        return roots

    def _sides(self, of: str, at: date) -> list[str | None]:
        for_, against = self.oracle.warrant_graph.sides(of, at)
        return [of, *for_, *against]

    def _stands(self, of: Any, at: date) -> list[str | None]:
        if isinstance(of, list | tuple):
            return [r for item in of for r in self._stands(item, at)]
        statement = self.rec.world.statements[of]
        return [of, *(x.id for x in self.rec.supporters(statement, at))]

    def _names(self, of: str, at: date) -> list[str | None]:
        """The namings and mergers of the entity, and of each entity merged into it, by a date."""
        w, identity = self.rec.world, self.oracle.identity_map
        target = identity.survivor(of, at)
        members = {e for e in w.entities if identity.survivor(e, at) == target}
        return self._naming_ops(
            at, lambda n: n.entity in members, lambda m: bool(members & set(m.entities))
        )

    def _referent(self, of: str, at: date) -> list[str | None]:
        """The namings that give the name, and the mergers of the entity that holds it."""
        referent = self.oracle.identity_map.referent(of, at)
        if referent is None:
            return []
        return self._naming_ops(at, lambda n: n.name == of, lambda m: referent in m.entities)

    def _naming_ops(
        self, at: date, naming: Callable[[Any], bool], merger: Callable[[Any], bool]
    ) -> list[str | None]:
        w = self.rec.world
        records = [n for n in w.namings if n.at <= at and naming(n)]
        records += [m for m in w.merges if m.at <= at and merger(m)]
        return [self.rec.op_id(r) for r in sorted(records, key=order)]


def artefacts(world: World, refs: Iterable[str], *, at: date | None = None) -> tuple[str, ...]:
    """The documents that carry the statements, or announce the operations, of ``refs``, in the
    order of the calendar and, inside one date, of filing."""
    wanted = set(refs)
    filed = {id_: n for n, id_ in enumerate(world.artefacts)}
    found = {a.id for ref in wanted for a in world.carriers(ref)}
    found |= {a.id for a in world.artefacts.values() if a.announces in wanted or a.id in wanted}
    if at is not None:
        found = {ref for ref in found if world.artefacts[ref].at <= at}
    return tuple(sorted(found, key=lambda id_: (world.artefacts[id_].at, filed[id_])))
