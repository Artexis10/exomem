"""The template renderer: the text of a world's documents, at one implicitness level, with no model.

Layers one and two of the world fix every document before any text exists: which statements and
announced operations it carries, its author, its date. The renderer chooses a surface form from the
bank for each of them, fills the form's slots from the world, and checks what it wrote. It changes
no record, so no key can change with the level.

**The level.** The round's `implicitness` dial is the level of the documents that carry the planted
change: the documents of the steps that apply in one world only. Every other document stays at
`stated`.

**What it checks.** A document that fails a check refuses the world, and the refusal names the
document and the surface form.

* *Anchors.* Every name, value and date of the statements that a document carries is in its text, and
  no value that a statement about the same attribute states, and that the document does not itself
  carry or announce, is in its text.
* *Explicit forms.* A document below `stated` holds no sentence that a `stated` form gives for the
  same statement.
* *Shared forms.* A probe's task is made from no form that an artefact of its thread uses.

A text is compared token by token: ``str.split``, with the punctuation at the ends of a token
stripped. A name or a value is present when its tokens appear in order.
"""

from __future__ import annotations

import hashlib
import string
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from random import Random
from typing import Any

from membench.ids import child_seed

from .core import BOTH, Artefact, Statement, World
from .loader import Catalogue, Refusal
from .manifest import dump
from .pack import AttributeSchema
from .planner import Plan, Thread
from .surface import LEVELS, STATED, Bank, Form


class RenderRefused(Exception):
    """The world cannot be rendered; every refusal is in ``refusals``, each naming its document."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


class _Unrenderable(Exception):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


# -- the anchor check: one function over a text, so a rewrite of the text can use it too -----------


def tokens(text: str) -> list[str]:
    return [t for word in text.split() if (t := word.strip(string.punctuation))]


def holds(text: str, phrase: str) -> bool:
    """Whether the tokens of ``phrase`` appear in ``text``, in order and side by side."""
    have, want = tokens(text), tokens(phrase)
    return bool(want) and any(
        have[i : i + len(want)] == want for i in range(len(have) - len(want) + 1)
    )


def anchor_problems(text: str, anchors: Iterable[str], forbidden: Iterable[str] = ()) -> list[str]:
    """What is wrong with ``text``: each anchor that is missing, and each forbidden value that is
    present. An anchor is a name, a value or a date that the statements of the text must show."""
    return [f"`{a}` is missing" for a in sorted(set(anchors)) if not holds(text, a)] + [
        f"`{v}` is a value that the document must not hold"
        for v in sorted(set(forbidden))
        if holds(text, v)
    ]


# -- the records of a rendering ---------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    ref: str  # the statement or announced operation that the line renders
    forms: tuple[str, ...]  # the surface form of the sentence, then that of each qualifier
    text: str


@dataclass(frozen=True)
class Document:
    artefact: str
    level: str
    lines: tuple[Line, ...]
    text: str

    @property
    def forms(self) -> tuple[str, ...]:
        return tuple(f for line in self.lines for f in line.forms)


@dataclass(frozen=True)
class Probe:
    thread: str
    form: str  # the probe form of the catalogue
    at: date
    pattern: str  # the surface form that made the task
    text: str


@dataclass(frozen=True)
class Rendering:
    level: str
    bank: Bank
    documents: Mapping[str, Document]
    probes: tuple[Probe, ...] = field(default_factory=tuple)

    def manifest(self) -> dict[str, Any]:
        """The level and the surface forms of every document, and a hash of every text."""
        return {
            "level": self.level,
            "bank": {"pack": self.bank.pack, "split": self.bank.split, "digest": self.bank.digest},
            "artefacts": [
                {"id": d.artefact, "level": d.level, "forms": list(d.forms), "hash": _hash(d.text)}
                for d in sorted(self.documents.values(), key=lambda d: d.artefact)
            ],
            "probes": [
                {
                    "thread": p.thread,
                    "form": p.form,
                    "at": p.at.isoformat(),
                    "pattern": p.pattern,
                    "hash": _hash(p.text),
                }
                for p in self.probes
            ],
        }

    def write(self, plan: Plan, directory: Path) -> str:
        """Write the world, its manifest with this rendering, one file for each document, and the
        probes' tasks into ``directory``. Returns the manifest's digest."""
        digest = plan.write(directory, self.manifest())
        (directory / "artefacts").mkdir(exist_ok=True)
        for document in self.documents.values():
            (directory / "artefacts" / f"{document.artefact}.txt").write_text(
                document.text, encoding="utf-8"
            )
        tasks = [
            {"thread": p.thread, "form": p.form, "at": p.at, "task": p.text} for p in self.probes
        ]
        (directory / "probes.json").write_text(dump(tasks), encoding="utf-8")
        return digest


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- reading a world into items that a form can fill -----------------------------------------------


@dataclass(frozen=True)
class _Item:
    """One statement or announced operation, as the slots that a form may fill."""

    ref: str
    key: str  # what a form serves, such as `statement.fact`
    fills: Mapping[str, str]
    optional: frozenset[str]  # the slots that only some items of this key fill
    schema: AttributeSchema | None  # the attribute of the `about` slot
    qualifiers: tuple[tuple[str, Mapping[str, str]], ...]
    anchors: tuple[str, ...]  # the names and values that the text must show
    allowed: frozenset[str]  # the values that the text may show
    about: str | None  # the attribute, event or entity that other statements may be about
    stated: tuple[str, ...] = ()  # further anchors that the text holds at `stated` only


class _Reader:
    def __init__(self, plan: Plan, world: World):
        self.plan, self.world = plan, world

    def name(self, ref: Any, at: date) -> str | None:
        """The name of an entity at a date, of an event or of a context, or ``None`` if ``ref`` is none."""
        if not isinstance(ref, str):
            return None
        backbone = self.plan.backbone
        if ref in self.world.entities:
            renamed = sorted(
                (
                    n
                    for n in self.world.namings
                    if n.entity == ref and n.replaces is not None and n.at <= at
                ),
                key=lambda n: (n.at, n.seq),
            )
            return renamed[-1].name if renamed else self.world.entities[ref].name
        if ref in backbone.events:
            return backbone.events[ref].name
        return backbone.context_names.get(ref)

    def text(self, value: Any, at: date) -> str:
        return self.name(value, at) or str(value)

    def where(self, ref: Any, at: date) -> tuple[str, AttributeSchema | None]:
        """The subject of an attribute, an event or an entity, and the attribute's schema."""
        if isinstance(ref, str) and ref in self.world.attributes:
            owner = self.name(self.world.attributes[ref].entity, at)
            return owner or ref, self.plan.backbone.attributes[ref].schema
        name = self.name(ref, at)
        if name is None:
            raise _Unrenderable(
                "no-subject", f"`{ref}` is no attribute, event or entity of the world"
            )
        return name, None

    def statement(self, s: Statement) -> _Item:
        subject, schema = self.where(s.about, s.at)
        by = self.text(s.by, s.at)
        fills = {"by": by, "subject": subject}
        anchors = [by, subject]
        optional = {"about", "value", "quoting", "as"}
        if schema is not None:
            fills["about"] = schema.label
        qualifiers: list[tuple[str, Mapping[str, str]]] = []
        if s.rule is not None:
            key, value = f"rule.{s.rule.shape}", s.rule.value
            optional.add("scope")
            if s.rule.context is not None:
                fills["scope"] = self.text(s.rule.context, s.at)
                anchors.append(fills["scope"])
        else:
            key, value = f"statement.{s.kind}", s.value
            if s.scope is not None:
                scope = self.text(s.scope, s.at)
                qualifiers.append(("qualifier.scope", {"by": by, "scope": scope}))
                anchors.append(scope)
        if value is not None:
            fills["value"] = self.text(value, s.at)
            anchors.append(fills["value"])
        for slot, extra in (("quoting", s.quoting), ("as", s.name_used)):
            if extra is not None:
                fills[slot] = self.text(extra, s.at)
                anchors.append(fills[slot])
        for family, word in (("hedge", s.hedge), ("basis", s.basis)):
            if word is not None:
                qualifiers.append((f"qualifier.{family}.{word}", {"by": by}))
        allowed = frozenset({fills["value"]} if "value" in fills else ())
        return _Item(
            s.id,
            key,
            fills,
            frozenset(optional),
            schema,
            tuple(qualifiers),
            tuple(anchors),
            allowed,
            s.about,
        )

    def operation(self, ref: str) -> _Item:
        name, record = self.world.operations[ref]
        if name == "name":
            fills = {"name": record.name}
            if record.replaces is not None:
                fills["former"] = record.replaces
            else:
                fills["subject"] = self.text(record.entity, record.at)
            return _Item(
                ref,
                "operation.name",
                fills,
                frozenset({"former", "subject"}),
                None,
                (),
                tuple(fills.values()),
                frozenset(),
                None,
            )
        if name == "merge":
            names = [self.text(e, record.at) for e in record.entities]
            fills = {"subject": names[0], "absorbed": " and ".join(names[1:])}
            return _Item(
                ref,
                "operation.merge",
                fills,
                frozenset(),
                None,
                (),
                tuple(names),
                frozenset(),
                None,
            )
        return self.act(ref, name, record)

    def act(self, ref: str, name: str, record: Any) -> _Item:
        """An act of one holder on a statement: `correct`, `withdraw`, `tell` or `restrict`."""
        at = record.at
        acted_on = next(
            getattr(record, f) for f in ("statement", "item", "target") if hasattr(record, f)
        )
        target = self.target(acted_on)
        if target is None and name != "tell":
            raise _Unrenderable("no-subject", f"`{ref}` acts on no statement of the world")
        by = target.by if name == "tell" and target is not None else getattr(record, "by", None)
        fills = {} if by is None else {"by": self.text(by, at)}
        if name == "tell":
            fills["holder"] = self.text(record.holder, at)
        subject, schema = self.where(target.about if target is not None else acted_on, at)
        fills["subject"] = subject
        if schema is not None:
            fills["about"] = schema.label
        content = None if target is None else (target.rule.value if target.rule else target.value)
        allowed: set[str] = set()
        stated: tuple[str, ...] = ()
        if content is not None and name in ("correct", "withdraw", "tell"):
            slot = "value" if name == "tell" else "old"
            fills[slot] = self.text(content, at)
            allowed.add(fills[slot])
            stated = (fills[slot],) if slot == "old" else ()
        if name == "correct":
            fills["value"] = self.text(record.value, at)
            allowed.add(fills["value"])
        if name == "restrict":
            if record.audience is not None:
                holders = self.plan.backbone.audiences.get(record.audience, ())
                fills["audience"] = ", ".join(sorted(self.text(h, at) for h in holders))
            if record.until is not None:
                fills["until"] = record.until.isoformat()
        anchors = tuple(v for slot, v in fills.items() if slot not in ("about", "old"))
        return _Item(
            ref,
            f"operation.{name}",
            fills,
            frozenset(
                {"about", "value", "audience", "until"} | ({"by"} if name == "tell" else set())
            ),
            schema,
            (),
            anchors,
            frozenset(allowed),
            None if target is None else target.about,
            stated,
        )

    def target(self, ref: Any) -> Statement | None:
        """The statement that an operation acts on: a statement, or the first one that a document carries."""
        if ref in self.world.statements:
            return self.world.statements[ref]
        artefact = self.world.artefacts.get(ref)
        return self.world.statements[artefact.carries[0]] if artefact and artefact.carries else None


# -- rendering --------------------------------------------------------------------------------------


def _levels(plan: Plan, world: World, level: str) -> dict[str, str]:
    """The level of each document: the round's level for the documents that carry the planted change,
    and `stated` for every other."""
    changing = {
        step["id"]
        for thread in plan.threads
        for step in thread.steps
        if "id" in step and step.get("world", BOTH) != BOTH
    }
    return {a.id: level if _source(a) in changing else STATED for a in world.artefacts.values()}


def _source(artefact: Artefact) -> str | None:
    """The statement or the announced operation that an artefact stands for."""
    return artefact.announces or (artefact.carries[0] if artefact.carries else None)


class _Renderer:
    def __init__(self, plan: Plan, catalogue: Catalogue, bank: Bank):
        self.plan, self.catalogue, self.bank = plan, catalogue, bank
        self.world = plan.world()
        self.reader = _Reader(plan, self.world)
        self.refusals: list[Refusal] = []

    def pick(self, pool: Collection[Form], label: str) -> Form:
        ordered = sorted(pool, key=lambda f: f.id)
        return ordered[Random(child_seed(self.plan.seed, label, 0)).randrange(len(ordered))]

    def phrase(self, item: _Item, level: str) -> str | None:
        """How the sentence names the attribute: its label, or at `paraphrased` one of its paraphrases."""
        if item.schema is None or level != "paraphrased":
            return None if item.schema is None else item.schema.label
        if not item.schema.paraphrases:
            raise _Unrenderable("no-paraphrase", f"attribute `{item.schema.id}` has no paraphrase")
        return self.pick_phrase(item)

    def pick_phrase(self, item: _Item) -> str:
        assert item.schema is not None
        phrases = item.schema.paraphrases
        return phrases[
            Random(child_seed(self.plan.seed, f"phrase:{item.ref}", 0)).randrange(len(phrases))
        ]

    def line(self, item: _Item, level: str) -> Line:
        fills = dict(item.fills)
        if (about := self.phrase(item, level)) is not None:
            fills["about"] = about
        pool = self.bank.eligible(item.key, level, fills, item.optional)
        if not pool:
            raise _Unrenderable(
                "no-form", f"no form serves `{item.key}` at `{level}` with {sorted(fills)}"
            )
        form = self.pick(pool, f"form:{item.ref}:{level}")
        sentence, used = form.text.format(**fills), [form.id]
        for key, extra in item.qualifiers:
            qualified = self.bank.eligible(key, level, {"text": sentence, **extra})
            if not qualified:
                raise _Unrenderable("no-form", f"no form serves `{key}` at `{level}`")
            wrapper = self.pick(qualified, f"form:{item.ref}:{key}:{level}")
            sentence = wrapper.text.format(text=sentence, **extra)
            used.append(wrapper.id)
        return Line(item.ref, tuple(used), sentence)

    def explicit(self, item: _Item) -> list[tuple[str, str]]:
        """Each sentence that a `stated` form gives for the item, with that form's id."""
        phrases = [None] if item.schema is None else [item.schema.label, *item.schema.paraphrases]
        found = []
        for phrase in phrases:
            fills = dict(item.fills) | ({} if phrase is None else {"about": phrase})
            for form in self.bank.eligible(item.key, STATED, fills, item.optional):
                found.append((form.id, form.text.format(**fills)))
        return found

    def forbidden(self, item: _Item, anchors: Collection[str]) -> set[str]:
        """The values that other statements about the item's attribute state, and the item does not."""
        if item.about is None:
            return set()
        others = {
            self.reader.text(s.rule.value if s.rule is not None else s.value, s.at)
            for s in self.world.statements_about(item.about)
            if (s.rule.value if s.rule is not None else s.value) is not None
        }
        return others - item.allowed - set(anchors)

    def document(self, artefact: Artefact, level: str) -> tuple[Document, list[_Item]]:
        if artefact.copy_of is not None:
            raise _Unrenderable("unsupported", "a copy has no form in the bank yet")
        if artefact.announces is not None:
            items = [self.reader.operation(artefact.announces)]
        else:
            items = [self.reader.statement(self.world.statements[s]) for s in artefact.carries]
        author = None if artefact.by is None else self.reader.text(artefact.by, artefact.at)
        frame = self.bank.frames[artefact.kind or ""]
        head = (frame.unsigned if author is None else frame.signed).format(
            by=author, date=artefact.at.isoformat()
        )
        lines = tuple(self.line(item, level) for item in items)
        text = "\n".join([head, *(line.text for line in lines)]) + "\n"
        return Document(artefact.id, level, lines, text), items

    def check(self, artefact: Artefact, document: Document, items: list[_Item]) -> None:
        anchors = {artefact.at.isoformat(), *(a for i in items for a in i.anchors)}
        if document.level == STATED:
            anchors |= {a for i in items for a in i.stated}
        if artefact.by is not None:
            anchors.add(self.reader.text(artefact.by, artefact.at))
        forbidden = {v for i in items for v in self.forbidden(i, anchors)}
        for problem in anchor_problems(document.text, anchors, forbidden):
            self.refuse("anchor", artefact.id, f"{problem}; forms {list(document.forms)}")
        if document.level == STATED:
            return
        for item, line in zip(items, document.lines, strict=True):
            for form_id, sentence in self.explicit(item):
                if holds(document.text, sentence):
                    self.refuse(
                        "explicit-form",
                        artefact.id,
                        f"the text at `{document.level}` holds the sentence of `stated` form `{form_id}`; forms {list(line.forms)}",
                    )

    def refuse(self, code: str, item: str, detail: str) -> None:
        self.refusals.append(Refusal(code, item, detail))

    def documents(self, level: str) -> dict[str, Document]:
        levels = _levels(self.plan, self.world, level)
        documents: dict[str, Document] = {}
        for artefact in sorted(self.world.artefacts.values(), key=lambda a: a.id):
            try:
                document, items = self.document(artefact, levels[artefact.id])
            except _Unrenderable as cannot:
                self.refuse(cannot.code, artefact.id, cannot.detail)
                continue
            self.check(artefact, document, items)
            documents[artefact.id] = document
        return documents

    # -- the tasks of the probes ------------------------------------------------------------------

    def subject(
        self, thread: Thread, spec: Mapping[str, Any], at: date
    ) -> tuple[str, AttributeSchema | None]:
        """What a probe asks about: the thing that its key reads, named as the world names it."""
        part = spec["value"]
        refs = [thread.roles[part["role"]]] if "role" in part else thread.resolve(part["of"])
        names = []
        for ref in refs if isinstance(refs, list) else [refs]:
            statement = self.world.statements.get(ref)
            ref = statement.about if statement is not None and statement.about else ref
            try:
                names.append(self.reader.where(ref, at))
            except _Unrenderable:
                names.append((str(ref), None))  # a name that the recipe gives, such as a new name
        if len(names) == 1:
            return names[0]
        return " and ".join(n for n, _ in names), None

    def probes(self, documents: Mapping[str, Document]) -> list[Probe]:
        thread_of = {
            step["id"]: thread.id
            for thread in self.plan.threads
            for step in thread.steps
            if "id" in step
        }
        used: dict[str, set[str]] = {}
        for artefact in self.world.artefacts.values():
            if artefact.id in documents and (thread := thread_of.get(_source(artefact) or "")):
                used.setdefault(thread, set()).update(documents[artefact.id].forms)
        probes = []
        for thread in self.plan.threads:
            entry, recipe = (
                self.catalogue.entries[thread.entry],
                self.catalogue.recipes[thread.recipe],
            )
            keys = (recipe.get("twin") or {}).get("keys", {}) if entry.twin_of else recipe["keys"]
            for form in entry.probes:
                at = thread.slots[keys[form]["at"]]
                subject, schema = self.subject(thread, keys[form], at)
                fills = {"subject": subject} | ({} if schema is None else {"about": schema.label})
                item = f"probe {thread.id} {form}"
                pool = self.bank.eligible(f"probe.{form}", None, fills, {"about"})
                free = [f for f in pool if f.id not in used.get(thread.id, set())]
                if not free:
                    self.refuse(
                        "shared-form" if pool else "no-form",
                        item,
                        f"every form for `probe.{form}` is one that an artefact of the thread uses: {sorted(f.id for f in pool)}"
                        if pool
                        else f"no form serves `probe.{form}` with {sorted(fills)}",
                    )
                    continue
                chosen = self.pick(free, f"probe:{thread.id}:{form}")
                probes.append(Probe(thread.id, form, at, chosen.id, chosen.text.format(**fills)))
        return probes


def render(plan: Plan, catalogue: Catalogue, bank: Bank, level: str) -> Rendering:
    """Render every document of the plan's shared world at ``level``, and the task of every probe.

    Raises ``RenderRefused`` with every refusal: a level that the renderer does not build, a bank for
    another pack or split, a document that no form fits, and a document or a probe that fails a check.
    """
    refusals = []
    if level not in LEVELS:
        refusals.append(
            Refusal(
                "undeclared-value",
                f"level {level}",
                "is no level that the template renderer builds",
            )
        )
    if bank.pack != plan.pack.id or bank.split != plan.split:
        refusals.append(
            Refusal(
                "wrong-bank",
                f"bank {bank.pack}/{bank.split}",
                f"cannot render a `{plan.split}` world of pack `{plan.pack.id}`: a form belongs to one split",
            )
        )
    if refusals:
        raise RenderRefused(refusals)
    renderer = _Renderer(plan, catalogue, bank)
    documents = renderer.documents(level)
    probes = renderer.probes(documents)
    if renderer.refusals:
        raise RenderRefused(renderer.refusals)
    return Rendering(level, bank, documents, tuple(probes))
