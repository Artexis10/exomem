"""The evidence layer as the oracle reads it at one date.

Every mechanism reads statements through ``Record``, so what counts as withdrawn, replaced or
defeated is decided once. Readings are pure: they take a date and change nothing.

A statement ends in one of three ways, and the earliest one counts:

* it is withdrawn;
* it is replaced: a later asserted statement of the same holder, about the same attribute and scope
  (the norm ``later-replaces``);
* it is defeated by a statement that is itself neither withdrawn nor replaced (the norm
  ``defeated-supports-nothing``). The defeater's own standing is read one hop deep, so two
  statements that defeat each other both end and no reading loops.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from .core import ASSERTED, Statement, World

# Closed values that the mechanisms implement: the commitment that asserts nothing, the volatility
# class that holds until a recorded change, the statement basis that passes a content on, and the
# rule shapes that fix a value in a context or name the holder that governs an attribute.
NO_COMMITMENT = "none"
DURABLE = "durable"
TOLD = "told"
GOVERNS = "governs"
DEFAULT = "default"
EXCEPTION = "exception"


class NotUnique(Exception):
    """The evidence leaves a reading more than one answer, so no key may rest on it."""

    def __init__(self, item: str, detail: str):
        self.item, self.detail = item, detail
        super().__init__(f"not-unique: {item}: {detail}")


class Ended(enum.Enum):
    WITHDRAWN = "withdrawn"
    REPLACED = "replaced"
    DEFEATED = "defeated"


@dataclass(frozen=True)
class End:
    kind: Ended
    ref: str  # the replacing or defeating statement; for a withdrawal, a label of the withdrawal
    at: date
    seq: int


def order(item: Any) -> tuple[date, int]:
    """The world's order of events: by date, and inside one date by the order of the calls."""
    return (item.at, item.seq)


class Record:
    def __init__(self, world: World):
        self.world = world

    def asserts(self, statement: Statement) -> bool:
        return self.world.commitment(statement) == ASSERTED

    def commits(self, statement: Statement) -> bool:
        """Whether the statement commits its holder to anything: asserted, proposed or reported."""
        commitment = self.world.commitment(statement)
        return commitment is not None and commitment != NO_COMMITMENT

    def content(self, statement: Statement, at: date) -> Any:
        """The content that the record holds on a date: the latest correction replaces the value."""
        corrected = [
            c for c in self.world.corrections if c.statement == statement.id and c.at <= at
        ]
        return max(corrected, key=order).value if corrected else statement.value

    def corrections(self, statement: Statement, at: date) -> list:
        return sorted(
            (c for c in self.world.corrections if c.statement == statement.id and c.at <= at),
            key=order,
        )

    def end(self, statement: Statement, at: date, defeats: bool = True) -> End | None:
        """The earliest way that the statement has ended by a date, or ``None`` if it stands."""
        w = self.world
        found = [
            End(Ended.WITHDRAWN, f"withdrawal-{statement.id}", x.at, x.seq)
            for x in w.withdrawals
            if x.statement == statement.id and x.at <= at
        ]
        if statement.rule is None and self.asserts(statement) and statement.about is not None:
            later = next(
                (
                    n
                    for n in w.statements_about(statement.about)
                    if n.at <= at
                    and order(n) > order(statement)
                    and n.by == statement.by
                    and n.scope == statement.scope
                    and n.rule is None
                    and self.asserts(n)
                ),
                None,
            )
            if later is not None:
                found.append(End(Ended.REPLACED, later.id, later.at, later.seq))
        if defeats:
            for edge in w.edges:
                d = w.statements.get(edge.source)
                if (
                    edge.mode is not None
                    and edge.target == statement.id
                    and d is not None
                    and d.at <= at
                    and self.end(d, at, defeats=False) is None
                ):
                    when = max(order(d), order(statement))
                    found.append(End(Ended.DEFEATED, d.id, *when))
        return min(found, key=lambda e: (e.at, e.seq)) if found else None

    def alive(self, statement: Statement, at: date) -> bool:
        """Made by the date, and neither withdrawn nor replaced. Defeat is not read."""
        return statement.at <= at and self.end(statement, at, defeats=False) is None

    def intact(self, statement: Statement, at: date) -> bool:
        """Made by the date, and neither withdrawn, replaced nor defeated."""
        return statement.at <= at and self.end(statement, at) is None

    def supporters(self, statement: Statement, at: date) -> list[Statement]:
        """The statements that support this one and are alive, one hop, in the order made."""
        return self._linked(statement, at, defeat=False)

    def defeaters(self, statement: Statement, at: date) -> list[Statement]:
        return self._linked(statement, at, defeat=True)

    def conclusions(self, statement: Statement, at: date) -> list[Statement]:
        """The statements that this one supports and that exist by the date."""
        w = self.world
        found = {
            e.target: w.statements[e.target]
            for e in w.edges
            if e.mode is None
            and e.source == statement.id
            and e.target in w.statements
            and w.statements[e.target].at <= at
        }
        return sorted(found.values(), key=order)

    def _linked(self, statement: Statement, at: date, *, defeat: bool) -> list[Statement]:
        w = self.world
        found = {
            e.source: w.statements[e.source]
            for e in w.edges
            if (e.mode is not None) == defeat
            and e.target == statement.id
            and e.source in w.statements
            and self.alive(w.statements[e.source], at)
        }
        return sorted(found.values(), key=order)

    def governing_rule(self, attribute: str, at: date) -> Statement | None:
        """The stated rule that makes one holder the governing one of an attribute, if any."""
        rules = [
            s
            for s in self.world.statements_about(attribute)
            if s.rule is not None and s.rule.shape == GOVERNS and self.alive(s, at)
        ]
        return rules[-1] if rules else None

    def governing(self, attribute: str, at: date) -> str | None:
        """The holder that a stated rule makes the governing one of an attribute, if any."""
        rule = self.governing_rule(attribute, at)
        return rule.rule.value if rule else None

    def op_id(self, record: Any) -> str | None:
        """The id of the operation that made a record (a correction, a naming, a telling...), which
        the document that announces the operation is filed under."""
        return next((i for i, (_, r) in self.world.operations.items() if r is record), None)

    def about(self, ref: str, at: date) -> list[Statement]:
        """The statements about an attribute, an event or an entity made by a date, in order."""
        return [s for s in self.world.statements_about(ref) if s.at <= at and s.rule is None]


def distinct(values: Iterable[Any]) -> list[Any]:
    """The values in the order first seen, without repeats."""
    seen: list[Any] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
