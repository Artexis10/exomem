"""Seven truth mechanisms in their smallest form, and the ``Oracle`` that reads them.

A mechanism is a pure reading of the evidence layer (``core.World``) on a date. A recipe's key part,
``{read: <mechanism>.<reading>, of: ..., where: ...}``, is one call of ``Oracle.read``. A reading
returns one answer, or raises ``NotUnique`` when the evidence leaves two, so a key that rests on it
is refused at generation. ``None`` is the answer "nothing holds"; it is never a zero.

``state_fold`` turns statements into membench ``StatusSpan`` records and asks membench's
``truth_at``, so the bitemporal rule lives in membench only. It lints the spans with membench's own
span lint and refuses to answer from a span that fails it.

Smallest form means: ``warrant_graph`` follows one hop of support and defeat; ``scope_lattice``
reads a chain of parent contexts and the tests use one level; ``corpus_closure`` reads whether a
statement that commits its holder is recorded. The readings that no slice 1 recipe reads are not
built, and ``Oracle.readings()`` lists the ones that are.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from membench.clock import EPOCH
from membench.oracle import lint_claim, truth_at
from membench.schema import (
    Assertion,
    ClaimRecord,
    ClaimStatus,
    SpanCause,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
)

from .core import PERMANENT, Statement, World
from .record import (
    DEFAULT,
    DURABLE,
    EXCEPTION,
    TOLD,
    End,
    Ended,
    NotUnique,
    Record,
    distinct,
    order,
)

# What ``state_fold.standing`` answers. The reading's definition in attributes.yaml fixes these words.
SETTLED, CONTESTED, WITHDRAWN, LAST_KNOWN, UNKNOWN = (
    "settled",
    "contested",
    "withdrawn",
    "last known",
    "unknown",
)

_WEEK_SECONDS = 7 * 86_400


class SpanLint(Exception):
    """membench's span lint refuses spans that ``state_fold`` derived."""


class NoWindow(Exception):
    """An attribute's volatility class goes stale, and no validity window was given for it."""


def _instant(at: date, seq: int) -> tuple[int, int]:
    """A world instant as membench's recorded week and sub-week offset.

    Inside one date the order of the calls (``seq``) orders the records, so membench never falls
    back to authoring order.
    """
    days = (at - EPOCH).days
    if days < 0:
        raise ValueError(f"{at} is before membench's epoch {EPOCH}")
    week, day = divmod(days, 7)
    return week, day * 86_400 + min(seq, 86_399)


def _knowledge(at: date) -> tuple[int, int]:
    """The end of a date, as the knowledge cutoff of a question asked on it."""
    week, offset = _instant(at, 0)
    return week, offset - offset % 86_400 + 86_399


@dataclass(frozen=True)
class Folded:
    """One content of one statement, as a membench claim, and the statement it came from."""

    claim: ClaimRecord
    value: Any
    statement: Statement
    version: int
    born: tuple[int, int]  # when this content entered the record
    ended: date | None


class StateFold:
    """The asserted statements about one attribute, as status spans under the entry's norms."""

    def __init__(self, record: Record, validity: Mapping[str, timedelta]):
        self.record = record
        self.validity = validity

    def fold(self, attribute: str, at: date, scope: str | None = None) -> list[Folded]:
        rec = self.record
        statements = [
            s
            for s in rec.world.statements_about(attribute)
            if s.at <= at and s.scope == scope and s.rule is None and rec.asserts(s)
        ]
        governing = rec.governing(attribute, at)
        if governing is not None:
            statements = [s for s in statements if s.by == governing]
        folded = [f for s in statements for f in self._versions(attribute, s, at)]
        self._lint(folded)
        return folded

    def _versions(self, attribute: str, s: Statement, at: date) -> list[Folded]:
        """A statement and each correction of it are separate claims. A correction replaces the
        earlier content for the whole period, so it supersedes it from the statement's own date."""
        rec = self.record
        contents = [(s.value, s.at, s.seq, f"SRC-{s.id}")] + [
            (c.value, c.at, c.seq, f"SRC-correction-{s.id}-{c.seq}") for c in rec.corrections(s, at)
        ]
        ids = [f"CLM-{s.id}-{i}" for i in range(len(contents))]
        end = rec.end(s, at)
        out = []
        for i, (value, vat, vseq, source) in enumerate(contents):
            week, offset = _instant(vat, vseq)
            spans = [
                self._span(ClaimStatus.CURRENT, s.at, vat, vseq, SpanCauseKind.INITIAL, source)
            ]
            assertions = {source: (Stance.SUPPORTS, vat, vseq)}
            superseded_by = None
            ended = None
            if i < len(contents) - 1:
                _, nat, nseq, _ = contents[i + 1]
                spans.append(
                    self._span(
                        ClaimStatus.SUPERSEDED,
                        s.at,
                        nat,
                        nseq,
                        SpanCauseKind.SUPERSESSION,
                        ids[i + 1],
                    )
                )
                superseded_by = ids[i + 1]
            elif end is not None:
                ended = end.at
                status, cause, by, stance = self._ending(end)
                spans.append(self._span(status, end.at, end.at, end.seq, cause, by))
                if stance is not None:
                    assertions[by] = (stance, end.at, end.seq)
                if end.kind is Ended.REPLACED:
                    superseded_by = by
            claim = ClaimRecord(
                claim_id=ids[i],
                subject=rec.world.attributes[attribute].entity,
                predicate=attribute,
                object=TypedValue(kind="text", value=str(value)),
                assertions=[
                    Assertion(
                        source_id=src,
                        stance=stance,
                        asserted_at=when,
                        recorded_week=_instant(when, seq)[0],
                        recorded_offset_s=_instant(when, seq)[1],
                    )
                    for src, (stance, when, seq) in assertions.items()
                ],
                status_timeline=spans,
                supersedes=ids[i - 1] if i else None,
                superseded_by=superseded_by,
            )
            out.append(Folded(claim, value, s, i, (week, offset), ended))
        return out

    @staticmethod
    def _span(
        status: ClaimStatus, valid_from: date, at: date, seq: int, cause: SpanCauseKind, by: str
    ) -> StatusSpan:
        week, offset = _instant(at, seq)
        return StatusSpan(
            status=status,
            valid_from=valid_from,
            recorded_week=week,
            recorded_offset_s=offset,
            cause=SpanCause(kind=cause, by=by),
        )

    @staticmethod
    def _ending(end: End) -> tuple[ClaimStatus, SpanCauseKind, str, Stance | None]:
        if end.kind is Ended.WITHDRAWN:
            return ClaimStatus.REVOKED, SpanCauseKind.RETRACTION, f"SRC-{end.ref}", Stance.RETRACTS
        if end.kind is Ended.REPLACED:
            return ClaimStatus.SUPERSEDED, SpanCauseKind.SUPERSESSION, f"CLM-{end.ref}-0", None
        return ClaimStatus.DISPROVED, SpanCauseKind.DISPROOF, f"SRC-{end.ref}", Stance.DISPUTES

    @staticmethod
    def _lint(folded: list[Folded]) -> None:
        known = {f.claim.claim_id for f in folded} | {
            a.source_id for f in folded for a in f.claim.assertions
        }
        errors = [e for f in folded for e in lint_claim(f.claim, frozenset(known))]
        if errors:
            raise SpanLint("; ".join(errors))

    def _views(self, attribute: str, at: date, scope: str | None):
        week, offset = _knowledge(at)
        return [
            (f, truth_at(f.claim, at, week, knowledge_offset_s=offset))
            for f in self.fold(attribute, at, scope)
        ]

    def _active(self, attribute: str, at: date, scope: str | None) -> list[Folded]:
        return sorted(
            (f for f, view in self._views(attribute, at, scope) if view.is_active),
            key=lambda f: f.born,
        )

    def current_fold(self, attribute: str, at: date, scope: str | None = None) -> Folded | None:
        """The claim that holds on a date, or ``None``. Two holders who disagree leave no answer."""
        active = self._active(attribute, at, scope)
        values = distinct(f.value for f in active)
        if len(values) > 1:
            raise NotUnique(
                attribute, f"holders disagree on {at} ({values}) and no stated rule governs"
            )
        return active[-1] if active else None

    def current(self, of: str, at: date, where: str | None = None) -> Any:
        fold = self.current_fold(of, at, where)
        return fold.value if fold else None

    def replaced(self, of: str, at: date, where: str | None = None) -> tuple:
        now = self.current(of, at, where)
        gone = sorted(
            (f for f, view in self._views(of, at, where) if not view.is_active),
            key=lambda f: (order(f.statement), f.version),
        )
        return tuple(distinct(f.value for f in gone if f.value != now))

    def history(self, of: str, at: date, where: str | None = None) -> tuple:
        folded = self.fold(of, at, where)
        week, offset = _knowledge(at)
        dates = sorted({f.statement.at for f in folded} | {f.ended for f in folded if f.ended})
        out: list[tuple[date, Any]] = []
        for d in dates:
            active = [
                f for f in folded if truth_at(f.claim, d, week, knowledge_offset_s=offset).is_active
            ]
            value = max(active, key=lambda f: f.born).value if active else None
            if not out or out[-1][1] != value:
                out.append((d, value))
        return tuple(out)

    def standing(self, of: str, at: date, where: str | None = None) -> str:
        folded = self.fold(of, at, where)
        if not folded:
            return UNKNOWN
        active = self._active(of, at, where)
        if not active:
            return WITHDRAWN
        if len(distinct(f.value for f in active)) > 1:
            return CONTESTED
        return LAST_KNOWN if self._stale(of, active[-1].statement, at) else SETTLED

    def _stale(self, attribute: str, statement: Statement, at: date) -> bool:
        volatility = self.record.world.attributes[attribute].volatility
        if volatility in (PERMANENT, DURABLE):
            return False
        if volatility not in self.validity:
            raise NoWindow(f"no validity window for volatility class `{volatility}`")
        return at - statement.at > self.validity[volatility]


class KindTag:
    """The kind and hedge that the world gave a statement, kept until another statement changes them."""

    def __init__(self, record: Record):
        self.record = record

    def deciding(self, of: str, at: date) -> Statement | None:
        """The statement itself, or of those about a subject the latest one that asserts and stands,
        and when none asserts, the latest one that stands."""
        w = self.record.world
        if of in w.statements:
            return w.statements[of]
        standing = [s for s in self.record.about(of, at) if self.record.intact(s, at)]
        asserting = [s for s in standing if self.record.asserts(s)]
        pool = asserting or standing
        return pool[-1] if pool else None

    def kind(self, of: str, at: date) -> str | None:
        s = self.deciding(of, at)
        return s.kind if s else None

    def hedge(self, of: str, at: date) -> str | None:
        s = self.deciding(of, at)
        return s.hedge if s else None


class HolderLedger:
    """Whose a statement is, and who asserts a content."""

    def __init__(self, record: Record):
        self.record = record

    def holders(self, of: str, at: date) -> tuple:
        """The holders whose statements about the subject assert it and still stand."""
        rec = self.record
        return tuple(
            distinct(s.by for s in rec.about(of, at) if rec.asserts(s) and rec.intact(s, at))
        )

    def holder(self, of: str, at: date, for_: Any = None) -> str | None:
        """Who a content is the holder's. Of several statements of one content the holder is the one
        who stated it first; a statement that quotes a holder, or whose basis is `told`, passes on
        that holder's content."""
        rec, w = self.record, self.record.world
        if of in w.statements:
            return self._passed_on(w.statements[of], at, frozenset())
        stated = [s for s in rec.about(of, at) if rec.commits(s)]
        contents = distinct(rec.content(s, at) for s in stated)
        if for_ is None and len(contents) > 1:
            raise NotUnique(of, f"{len(contents)} contents are stated ({contents}); name one")
        wanted = contents[0] if for_ is None and contents else for_
        first = next((s for s in stated if rec.content(s, at) == wanted), None)
        return self._passed_on(first, at, frozenset()) if first else None

    def _passed_on(self, s: Statement, at: date, seen: frozenset[str]) -> str:
        rec, w = self.record, self.record.world
        if s.quoting is not None:
            return s.quoting
        if s.basis != TOLD:
            return s.by
        told = [
            w.statements[t.item]
            for t in w.tellings
            if t.holder == s.by
            and t.at <= s.at
            and t.item in w.statements
            and t.item not in seen
            and w.statements[t.item].about == s.about
            and rec.content(w.statements[t.item], at) == rec.content(s, at)
        ]
        if not told:
            raise NotUnique(s.id, "its basis is told and no telling of that content is recorded")
        return self._passed_on(min(told, key=order), at, seen | {s.id})


class IdentityMap:
    """Which entity a name refers to on a date. The first entity of a merge survives."""

    def __init__(self, record: Record):
        self.record = record

    def _survivor(self, entity: str, at: date) -> str:
        merges = sorted(self.record.world.merges, key=order)
        seen = {entity}
        while True:
            m = next((m for m in merges if m.at <= at and entity in m.entities[1:]), None)
            if m is None or m.entities[0] in seen:
                return entity
            entity = m.entities[0]
            seen.add(entity)

    def _held(self, entity: str, at: date) -> tuple:
        """The names that an entity holds on a date. A name that a later name replaced is not held."""
        w = self.record.world
        e = w.entities[entity]
        if e.start > at:
            return ()
        given = [n for n in sorted(w.namings, key=order) if n.entity == entity and n.at <= at]
        gone = {n.replaces for n in given if n.replaces is not None}
        return tuple(distinct(n for n in [e.name, *(g.name for g in given)] if n not in gone))

    def referent(self, of: str, at: date) -> str | None:
        found = distinct(
            self._survivor(e, at) for e in self.record.world.entities if of in self._held(e, at)
        )
        if len(found) > 1:
            raise NotUnique(of, f"names {found} on {at}")
        return found[0] if found else None

    def names(self, of: str, at: date) -> tuple:
        """Every name that the entity, or an entity merged into it, has held by a date, replaced
        names included."""
        w = self.record.world
        target = self._survivor(of, at)
        members = [
            e for e in w.entities if self._survivor(e, at) == target and w.entities[e].start <= at
        ]
        return tuple(
            distinct(
                name
                for e in members
                for name in (
                    w.entities[e].name,
                    *(n.name for n in sorted(w.namings, key=order) if n.entity == e and n.at <= at),
                )
            )
        )


class ScopeLattice:
    """A statement holds in the context where it was made, and the most specific context decides."""

    def __init__(self, record: Record):
        self.record = record

    def _chain(self, context: str | None) -> list[str]:
        chain: list[str] = []
        while context is not None and context not in chain:
            chain.append(context)
            context = self.record.world.contexts[context].parent
        return chain

    @staticmethod
    def _context(s: Statement) -> str | None:
        return s.rule.context if s.rule is not None else s.scope

    def _fixes_value(self, s: Statement) -> bool:
        if s.rule is not None:
            return s.rule.shape in (DEFAULT, EXCEPTION)
        return self.record.asserts(s)

    def deciding(self, attribute: str, where: str | None, at: date) -> Statement | None:
        candidates = [
            s
            for s in self.record.world.statements_about(attribute)
            if s.at <= at and self._fixes_value(s) and self.record.intact(s, at)
        ]
        for context in [*self._chain(where), None]:
            here = [s for s in candidates if self._context(s) == context]
            if here:
                return here[-1]
        return None

    def value_in(self, of: str, at: date, where: str | None = None) -> Any:
        s = self.deciding(of, where, at)
        if s is None:
            return None
        return s.rule.value if s.rule is not None else self.record.content(s, at)

    def applies(self, of: str, at: date, where: str | None = None) -> bool:
        """Whether a statement, or a rule, is the one that decides in the context."""
        s = self.record.world.statements[of]
        decider = self.deciding(s.about, where, at) if s.about is not None else None
        return decider is not None and decider.id == s.id


class WarrantGraph:
    """Support and defeat edges, one hop. A statement stands while it is intact and, if anything
    supports it, while one supporter is alive."""

    def __init__(self, record: Record, fold: StateFold):
        self.record = record
        self.fold = fold

    def _statement(self, ref: str) -> Statement:
        try:
            return self.record.world.statements[ref]
        except KeyError:
            raise ValueError(f"`{ref}` is no statement of the world") from None

    def _stands(self, s: Statement, at: date) -> bool:
        rec = self.record
        if not rec.intact(s, at):
            return False
        supported = any(e.mode is None and e.target == s.id for e in rec.world.edges)
        return not supported or bool(rec.supporters(s, at))

    def stands(self, of: str | list | tuple, at: date) -> bool | tuple:
        if isinstance(of, list | tuple):
            return tuple(self.stands(item, at) for item in of)
        return self._stands(self._statement(of), at)

    def _after_loss(self, of: str, at: date, survives: bool) -> tuple:
        """The conclusions of a statement that has ended: those that keep a support path, or lose all."""
        rec = self.record
        s = self._statement(of)
        if s.at > at or rec.end(s, at) is None:
            return ()
        return tuple(
            c.id
            for c in rec.conclusions(s, at)
            if rec.intact(c, at) and bool(rec.supporters(c, at)) == survives
        )

    def fallen(self, of: str, at: date) -> tuple:
        return self._after_loss(of, at, survives=False)

    def surviving(self, of: str, at: date) -> tuple:
        return self._after_loss(of, at, survives=True)

    def basis(self, of: str, at: date) -> tuple:
        """What a statement, or the standing value of an attribute, rests on: its supporters, or itself."""
        w = self.record.world
        if of in w.statements:
            s = w.statements[of]
        else:
            current = self.fold.current_fold(of, at)
            if current is None:
                return ()
            s = current.statement
        return tuple(x.id for x in self.record.supporters(s, at)) or (s.id,)

    def origins(self, of: str, at: date) -> tuple:
        """The holders at the roots of the standing statements about an attribute: a statement with
        a supporter has the supporters' holders as its origin, and no other has its own holder."""
        rec = self.record
        roots: list[str] = []
        for s in rec.about(of, at):
            if rec.asserts(s) and rec.intact(s, at):
                roots += [x.by for x in rec.supporters(s, at)] or [s.by]
        return tuple(distinct(roots))

    def sides(self, of: str, at: date) -> tuple:
        """The statements for a claim and the statements against it, as two tuples."""
        s = self._statement(of)
        rec = self.record
        return (
            tuple(x.id for x in rec.supporters(s, at)),
            tuple(x.id for x in rec.defeaters(s, at)),
        )


class CorpusClosure:
    """Whether anything is recorded."""

    def __init__(self, record: Record):
        self.record = record

    def recorded(self, of: str, at: date) -> bool:
        """Whether a statement that commits its holder bears on the subject. A question, a
        supposition or an example commits nothing and does not count."""
        return any(self.record.commits(s) for s in self.record.about(of, at))


class Oracle:
    """Read one world through its mechanisms. ``validity`` gives, for each volatility class that
    goes stale, how long an observation holds; a class that holds until a change needs none."""

    def __init__(self, world: World, validity: Mapping[str, timedelta] | None = None):
        self.record = Record(world)
        self.state_fold = StateFold(self.record, validity or {})
        self.kind_tag = KindTag(self.record)
        self.holder_ledger = HolderLedger(self.record)
        self.identity_map = IdentityMap(self.record)
        self.scope_lattice = ScopeLattice(self.record)
        self.warrant_graph = WarrantGraph(self.record, self.state_fold)
        self.corpus_closure = CorpusClosure(self.record)
        self._readings: dict[str, Callable[..., Any]] = {
            "corpus_closure.recorded": self.corpus_closure.recorded,
            "holder_ledger.holder": self.holder_ledger.holder,
            "holder_ledger.holders": self.holder_ledger.holders,
            "identity_map.names": self.identity_map.names,
            "identity_map.referent": self.identity_map.referent,
            "kind_tag.hedge": self.kind_tag.hedge,
            "kind_tag.kind": self.kind_tag.kind,
            "scope_lattice.applies": self.scope_lattice.applies,
            "scope_lattice.value_in": self.scope_lattice.value_in,
            "state_fold.current": self.state_fold.current,
            "state_fold.history": self.state_fold.history,
            "state_fold.replaced": self.state_fold.replaced,
            "state_fold.standing": self.state_fold.standing,
            "warrant_graph.basis": self.warrant_graph.basis,
            "warrant_graph.fallen": self.warrant_graph.fallen,
            "warrant_graph.origins": self.warrant_graph.origins,
            "warrant_graph.sides": self.warrant_graph.sides,
            "warrant_graph.stands": self.warrant_graph.stands,
            "warrant_graph.surviving": self.warrant_graph.surviving,
        }

    def readings(self) -> tuple[str, ...]:
        """The readings that are built, as ``mechanism.reading``."""
        return tuple(sorted(self._readings))

    def read(self, reading: str, *, of: Any, at: date, **arguments: Any) -> Any:
        """One reading, on a date. ``arguments`` are the reading's own: `where`, `for_`."""
        if reading not in self._readings:
            raise KeyError(f"`{reading}` is not built")
        return self._readings[reading](of, at, **arguments)

    # -- the carry rule and the trap rule of the recipes, read from the same record ---------------

    def bearers(self, role: Any, at: date) -> list[Statement]:
        """The statements of the world that carry a role: a `state` or `correct` step gives the role
        as its value or, for a holder, the role makes the statement. A rule carries no value."""
        w = self.record.world
        found = {
            s.id: s
            for s in w.statements.values()
            if s.at <= at and s.rule is None and (s.value == role or s.by == role)
        }
        for c in w.corrections:
            if c.at <= at and (c.value == role or c.by == role):
                found[c.statement] = w.statements[c.statement]
        return sorted(found.values(), key=order)

    def role_trap(self, role: Any, at: date, planted_in_mutation: bool = False) -> bool:
        """Whether a role is a trap. It is one where a statement carries it. In a thread planted in the
        mutation world a statement that asserts and stands makes its content no error."""
        carriers = self.bearers(role, at)
        if planted_in_mutation and any(
            self.record.asserts(s) and self.warrant_graph.stands(s.id, at) for s in carriers
        ):
            return False
        return bool(carriers)
