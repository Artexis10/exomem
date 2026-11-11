"""The world validator: check a finished world against its invariants, and name each fault.

``validate`` returns every fault as a ``Refusal`` (loader's shape: a code, the offending item and a
detail). ``check`` raises them together. The invariants, by code:

* ``undeclared-value``: a statement kind, hedge or basis, a rule shape, a defeat mode or a volatility
  class that the catalogue does not declare.
* ``unknown-reference``: a record names an id that is missing or of the wrong sort.
* ``unresolved-artefact``: a copy or a removal names an artefact that does not exist, or a statement
  that not exactly one artefact carries.
* ``date-before-epoch``: a date earlier than membench's epoch, which its status spans cannot hold.
* ``one-value-at-a-time``: an attribute takes two values on one date in one scope.
* ``event-placed-twice``: an event is set at two dates.
* ``end-of-nothing``: `end` on an attribute that holds no value or on an entity that is gone.
* ``permanent-changed``: a permanent attribute is set twice or ended.
* ``outside-existence``: a value, name or statement dated before its entity begins, or a value or
  name dated after the entity ended or was absorbed by a merge.
* ``acts-before-target``: a withdrawal, correction, telling, restriction, copy, removal or draw dated
  before the statement or artefact that it acts on, or an artefact dated before a statement it carries.
* ``not-a-holder``: something that is no holder makes a statement, is told one or restricts one.
* ``statement-before-event``: a statement that asserts or reports an event, dated before the event.
* ``effect-before-cause``: a cause dated after its effect.
* ``edge-to-self``: a support or defeat edge from a statement to itself.
* ``restriction-window``: a restriction that ends no later than it begins.
* ``merge-of-fewer-than-two``: a merge that does not join two distinct entities.
* ``name-not-held``: a statement that uses a name which its subject never held.
* ``draw-without-chance``: a draw whose chance is no statement of the chance rule shape.
* ``context-cycle``: a context that is its own ancestor.

The checks read closed values only: commitments, volatility classes and rule shapes.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

from membench.clock import EPOCH

from .core import ASSERTED, CHANCE, PERMANENT, REPORTED, Statement, World
from .loader import Refusal


class WorldInvalid(Exception):
    """The world breaks at least one invariant; every fault is in ``refusals``."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


def check(world: World) -> None:
    """Raise ``WorldInvalid`` unless the world is sound."""
    refusals = validate(world)
    if refusals:
        raise WorldInvalid(refusals)


def validate(world: World) -> list[Refusal]:
    return _Validator(world).run()


class _Validator:
    def __init__(self, world: World):
        self.w = world
        self.refusals: list[Refusal] = []
        self.placed = self._event_dates()
        self.ended = {
            a.target: a.at for a in world.assignments if a.ends and a.target in world.entities
        }
        self.absorbed = {
            entity: merge.at for merge in world.merges for entity in merge.entities[1:]
        }

    def refuse(self, code: str, item: str, detail: str) -> None:
        self.refusals.append(Refusal(code, item, detail))

    def run(self) -> list[Refusal]:
        for check in (
            self.declared_values,
            self.contexts,
            self.assignments,
            self.statements,
            self.acts,
            self.edges,
            self.restrictions,
            self.merges,
            self.names,
            self.artefacts,
            self.causes_and_draws,
            self.epoch,
        ):
            check()
        return self.refusals

    # -- lookups -----------------------------------------------------------------------------

    def _event_dates(self) -> dict[str, date]:
        return {
            a.target: a.at
            for a in sorted(self.w.assignments, key=lambda a: (a.at, a.seq))
            if a.target in self.w.events and not a.ends
        }

    def date_of(self, ref: str) -> date | None:
        """When a statement was made or an event happened, if the world says."""
        if ref in self.w.statements:
            return self.w.statements[ref].at
        return self.placed.get(ref)

    def entity_of(self, ref: str | None) -> str | None:
        if ref in self.w.attributes:
            return self.w.attributes[ref].entity
        return ref if ref in self.w.entities else None

    def reference(self, item: str, field: str, ref: Any, *sorts: str) -> bool:
        """Check that ``ref`` is an id in one of the named registries. ``sorts`` are World fields."""
        if ref is not None and any(ref in getattr(self.w, sort) for sort in sorts):
            return True
        self.refuse("unknown-reference", item, f"{field} `{ref}` is not one of: {', '.join(sorts)}")
        return False

    def holder(self, item: str, field: str, ref: Any) -> None:
        if ref in self.w.entities:
            if not self.w.entities[ref].holder:
                self.refuse("not-a-holder", item, f"{field} `{ref}` is not a holder")
        else:
            self.reference(item, field, ref, "entities")

    # -- the checks ----------------------------------------------------------------------------

    def declared_values(self) -> None:
        vocab = self.w.vocabulary
        for a in self.w.attributes.values():
            if a.volatility not in vocab.volatility_classes:
                self.refuse(
                    "undeclared-value", a.id, f"volatility `{a.volatility}` is not declared"
                )
        for s in self.w.statements.values():
            if s.rule is not None:
                if s.rule.shape not in vocab.rule_shapes:
                    self.refuse("undeclared-value", s.id, f"shape `{s.rule.shape}` is not declared")
                continue
            if s.kind not in vocab.commitments:
                self.refuse("undeclared-value", s.id, f"kind `{s.kind}` is not declared")
            if s.hedge is not None and s.hedge not in vocab.hedges:
                self.refuse("undeclared-value", s.id, f"hedge `{s.hedge}` is not declared")
            if s.basis is not None and s.basis not in vocab.bases:
                self.refuse("undeclared-value", s.id, f"basis `{s.basis}` is not declared")
        for e in self.w.edges:
            if e.mode is not None and e.mode not in vocab.defeat_modes:
                self.refuse("undeclared-value", e.source, f"defeat mode `{e.mode}` is not declared")

    def contexts(self) -> None:
        for c in self.w.contexts.values():
            if c.parent is not None:
                self.reference(c.id, "parent", c.parent, "contexts")
            seen, current = set(), c.id
            while current is not None and current in self.w.contexts:
                if current in seen:
                    self.refuse("context-cycle", c.id, "the context is its own ancestor")
                    break
                seen.add(current)
                current = self.w.contexts[current].parent
        for a in self.w.audiences.values():
            for member in sorted(a.holders):
                self.holder(a.id, "member", member)

    def assignments(self) -> None:
        w = self.w
        by_target: dict[tuple[str, str | None], list] = {}
        for a in sorted(w.assignments, key=lambda a: (a.at, a.seq)):
            if a.ends:
                self.reference(a.target, "end target", a.target, "attributes", "entities")
            else:
                self.reference(a.target, "set target", a.target, "attributes", "events")
            if a.scope is not None:
                self.reference(a.target, "scope", a.scope, "contexts")
            by_target.setdefault((a.target, a.scope), []).append(a)
        for (target, _scope), items in by_target.items():
            if target in w.events:
                placed = {a.at for a in items if not a.ends}
                if len(placed) > 1:
                    self.refuse("event-placed-twice", target, f"set at {len(placed)} dates")
                continue
            holding, count, previous = False, 0, None
            for a in items:
                if previous is not None and previous.at == a.at:
                    self.refuse("one-value-at-a-time", target, f"two assignments on {a.at}")
                previous = a
                if a.ends and target in w.entities:
                    if count:
                        self.refuse("end-of-nothing", target, f"the entity is gone before {a.at}")
                    count += 1
                elif a.ends:
                    if not holding:
                        self.refuse("end-of-nothing", target, f"no value holds on {a.at}")
                    holding = False
                else:
                    holding = True
                    self.existence(target, a.at, "a value")
            if target in w.attributes and w.attributes[target].volatility == PERMANENT:
                if len(items) > 1:
                    self.refuse("permanent-changed", target, "a permanent attribute changes")

    def existence(self, ref: str, when: date, what: str, ends_matter: bool = True) -> None:
        """Check that a thing which belongs to an entity is dated while the entity exists."""
        entity = self.entity_of(ref)
        if entity is None or entity not in self.w.entities:
            return
        if when < self.w.entities[entity].start:
            self.refuse("outside-existence", ref, f"{what} on {when} is before {entity} begins")
        if ends_matter:
            for gone in (self.ended, self.absorbed):
                if entity in gone and when > gone[entity]:
                    self.refuse(
                        "outside-existence", ref, f"{what} on {when} is after {entity} is gone"
                    )

    def statements(self) -> None:
        w = self.w
        for s in w.statements.values():
            self.holder(s.id, "by", s.by)
            if s.quoting is not None:
                self.holder(s.id, "quoting", s.quoting)
            if s.scope is not None:
                self.reference(s.id, "scope", s.scope, "contexts")
            if s.rule is not None:
                self.reference(s.id, "over", s.rule.over, "attributes", "events", "entities")
                if s.rule.context is not None:
                    self.reference(s.id, "context", s.rule.context, "contexts")
            elif self.reference(s.id, "about", s.about, "attributes", "events", "entities"):
                self.existence(s.about, s.at, "a statement", ends_matter=False)
                self.before_event(s)

    def before_event(self, s: Statement) -> None:
        if s.about in self.placed and self.w.commitment(s) in (ASSERTED, REPORTED):
            if s.at < self.placed[s.about]:
                self.refuse(
                    "statement-before-event",
                    s.id,
                    f"made on {s.at}, before `{s.about}` happens on {self.placed[s.about]}",
                )

    def acts(self) -> None:
        """Withdrawals, corrections, tellings and restrictions act on what already exists."""
        w = self.w
        for kind, items in (("withdraw", w.withdrawals), ("correct", w.corrections)):
            for a in items:
                item = f"{kind} {a.statement}"
                self.holder(item, "by", a.by)
                self.target_date(item, a.statement, a.at, "statements")
        for t in w.tellings:
            item = f"tell {t.item}"
            self.holder(item, "holder", t.holder)
            self.target_date(item, t.item, t.at, "statements", "events")
        for r in w.restrictions:
            item = f"restrict {r.target}"
            self.holder(item, "by", r.by)
            self.target_date(item, r.target, r.at, "statements", "artefacts")
            if r.audience is not None:
                self.reference(item, "audience", r.audience, "audiences")
        for n in w.namings:
            if self.reference(f"name {n.name}", "entity", n.entity, "entities"):
                self.existence(n.entity, n.at, f"the name `{n.name}`")
                if n.replaces is not None and n.replaces not in self.names_of(n.entity):
                    self.refuse(
                        "name-not-held", f"name {n.name}", f"`{n.replaces}` is no name of it"
                    )

    def target_date(self, item: str, ref: str, when: date, *sorts: str) -> None:
        if not self.reference(item, "target", ref, *sorts):
            return
        born = self.date_of(ref) if ref not in self.w.artefacts else self.w.artefacts[ref].at
        if born is not None and when < born:
            self.refuse("acts-before-target", item, f"dated {when}, before `{ref}` on {born}")

    def edges(self) -> None:
        for e in self.w.edges:
            item = f"{'defeat' if e.mode else 'support'} {e.source}"
            ok = self.reference(item, "from", e.source, "statements")
            ok = self.reference(item, "to", e.target, "statements") and ok
            if ok and e.source == e.target:
                self.refuse("edge-to-self", item, "the edge joins a statement to itself")

    def restrictions(self) -> None:
        for r in self.w.restrictions:
            if r.until is not None and r.until <= r.at:
                self.refuse(
                    "restriction-window", f"restrict {r.target}", f"ends {r.until}, begins {r.at}"
                )

    def merges(self) -> None:
        for m in self.w.merges:
            item = f"merge {', '.join(m.entities)}"
            if len(set(m.entities)) < 2:
                self.refuse("merge-of-fewer-than-two", item, "a merge joins two distinct entities")
            for entity in m.entities:
                if self.reference(item, "entity", entity, "entities"):
                    self.existence(entity, m.at, "the merge")

    def names_of(self, entity: str, by: date | None = None) -> set[str]:
        """The names that an entity began with or was given, by a date when one is named."""
        w = self.w
        given = (n.name for n in w.namings if n.entity == entity and (by is None or n.at <= by))
        return {w.entities[entity].name, *given}

    def names(self) -> None:
        """A statement may use any name that its subject, or an entity merged into it, ever held."""
        w = self.w
        for s in w.statements.values():
            if s.name_used is None:
                continue
            subject = self.entity_of(s.about)
            if subject is None:
                continue
            joined = {subject} | {
                e for m in w.merges if m.entities[0] == subject and m.at <= s.at for e in m.entities
            }
            held = {name for e in joined if e in w.entities for name in self.names_of(e, s.at)}
            if s.name_used not in held:
                self.refuse(
                    "name-not-held", s.id, f"`{s.name_used}` is no name of `{subject}` by {s.at}"
                )

    def artefacts(self) -> None:
        w = self.w
        for a in w.artefacts.values():
            if a.by is not None:
                self.holder(a.id, "by", a.by)
            for carried in a.carries:
                if self.reference(a.id, "carries", carried, "statements"):
                    if a.at < w.statements[carried].at:
                        self.refuse(
                            "acts-before-target",
                            a.id,
                            f"dated {a.at}, before `{carried}` on {w.statements[carried].at}",
                        )
            if a.copy_of is not None:
                source = w.resolve_artefact(a.copy_of)
                if source is None:
                    self.refuse("unresolved-artefact", a.id, f"`{a.copy_of}` names no one artefact")
                elif a.at < source.at:
                    self.refuse(
                        "acts-before-target",
                        a.id,
                        f"dated {a.at}, before `{source.id}` on {source.at}",
                    )
        for r in w.removals:
            source = w.resolve_artefact(r.artefact)
            if source is None:
                self.refuse("unresolved-artefact", f"remove {r.artefact}", "names no one artefact")
            elif r.at < source.at:
                self.refuse(
                    "acts-before-target",
                    f"remove {r.artefact}",
                    f"dated {r.at}, before `{source.id}` on {source.at}",
                )

    def causes_and_draws(self) -> None:
        w = self.w
        for c in w.causes:
            item = f"cause {c.source}"
            ok = self.reference(item, "from", c.source, "statements", "events")
            ok = self.reference(item, "to", c.target, "statements", "events") and ok
            if ok:
                cause_at, effect_at = self.date_of(c.source), self.date_of(c.target)
                if cause_at and effect_at and effect_at < cause_at:
                    self.refuse(
                        "effect-before-cause",
                        item,
                        f"`{c.target}` on {effect_at}, cause {cause_at}",
                    )
        for d in w.draws:
            item = f"draw {d.target}"
            self.reference(item, "target", d.target, "events")
            if d.chance in w.statements:
                rule = w.statements[d.chance].rule
                if rule is None or rule.shape != CHANCE:
                    self.refuse("draw-without-chance", item, f"`{d.chance}` states no chance")
                self.target_date(item, d.chance, d.at, "statements")
            else:
                self.refuse("draw-without-chance", item, f"`{d.chance}` is no statement")

    def epoch(self) -> None:
        w = self.w
        dated: Iterable[tuple[str, date]] = (
            [(e.id, e.start) for e in w.entities.values()]
            + [(a.target, a.at) for a in w.assignments]
            + [(s.id, s.at) for s in w.statements.values()]
            + [(a.id, a.at) for a in w.artefacts.values()]
            + [
                (f"{kind} {x.statement}", x.at)
                for kind, items in (("withdraw", w.withdrawals), ("correct", w.corrections))
                for x in items
            ]
            + [(f"name {n.name}", n.at) for n in w.namings]
            + [(f"tell {t.item}", t.at) for t in w.tellings]
            + [(f"draw {d.target}", d.at) for d in w.draws]
            + [(f"restrict {r.target}", r.at) for r in w.restrictions]
            + [(f"remove {r.artefact}", r.at) for r in w.removals]
            + [(f"merge {m.entities[0]}", m.at) for m in w.merges]
        )
        for item, when in dated:
            if when < EPOCH:
                self.refuse("date-before-epoch", item, f"{when} is before membench's epoch {EPOCH}")
