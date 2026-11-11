"""The bank of surface forms: the text patterns that the template renderer fills.

A surface form is one text pattern with slots, such as ``{by} wrote that {about} for {subject} is
{value}``. A bank is YAML data under ``packs/``, one file for one pack and one split. This module
reads the file and refuses what it cannot use, by name, as the pack loader does. It holds no word of
any pattern: every pattern comes from the bank file.

A form lists what it ``serves``, and each entry names a family and an item of a registry:

* ``statement.<kind>``: a statement of that kind (``statement-kinds.yaml``);
* ``rule.<shape>``: a rule of that shape;
* ``operation.<name>``: the document that announces an operation that the pack gives a document;
* ``qualifier.scope``, ``qualifier.hedge.<word>``, ``qualifier.basis.<value>``: a wrapper that takes
  the sentence as ``{text}``;
* ``probe.<form>``: the task of a probe form.

A form fits an item when every slot of the form is one that the item can fill, and the form holds
every slot that the item fills only some of the time. A statement that has a value therefore gets a
form that shows the value, and a question that has none gets a form that has no ``{value}``.
"""

from __future__ import annotations

import hashlib
import string
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core import Vocabulary
from .loader import Refusal
from .pack import Pack

# Closed values that this module and the renderer implement: the implicitness levels that the
# template renderer builds, and the one that every other level is read against.
LEVELS = ("stated", "paraphrased", "unmarked")
STATED = "stated"
# The slots that a text may hold. A document frame takes `by` and `date`.
SLOTS = frozenset(
    "by subject about value quoting as scope old name former holder absorbed audience until text".split()
)
FRAME_SLOTS = frozenset({"by", "date"})
# The families of what a form serves, each with the registry that its item is read from.
FAMILIES = ("statement", "rule", "operation", "qualifier", "probe")
_QUALIFIER_TEXT = "text"

_FIELDS = {
    "bank": ("schema_version", "pack", "split", "describes", "frames", "forms"),
    "frame": ("signed", "unsigned"),
    "form": ("id", "serves", "level", "text"),
}


class BankRefused(Exception):
    """The bank holds at least one refusal; every one is in ``refusals``."""

    def __init__(self, refusals: list[Refusal]):
        self.refusals = refusals
        super().__init__("\n".join(str(r) for r in refusals))


@dataclass(frozen=True)
class Form:
    id: str
    serves: tuple[str, ...]
    level: str | None  # a form that serves only probes has none
    text: str
    slots: frozenset[str]


@dataclass(frozen=True)
class Frame:
    """The head of a document of one artefact kind: signed when an author is known."""

    signed: str
    unsigned: str


@dataclass(frozen=True)
class Bank:
    pack: str
    split: str
    digest: str
    frames: Mapping[str, Frame]
    forms: tuple[Form, ...]
    _served: Mapping[str, tuple[Form, ...]] = field(default_factory=dict, repr=False)

    def eligible(
        self,
        key: str,
        level: str | None,
        fills: Collection[str],
        optional: Collection[str] = (),
    ) -> list[Form]:
        """The forms that serve ``key`` at ``level`` and fit an item that fills ``fills``, of which
        ``optional`` are the slots that only some items fill. A ``level`` of ``None`` asks for any."""
        present = set(fills)
        shown = set(optional) & present
        return [
            form
            for form in self._served.get(key, ())
            if (level is None or form.level == level)
            and form.slots <= present
            and shown <= form.slots
        ]


def load_bank(path: Path, vocabulary: Vocabulary, pack: Pack) -> Bank:
    """Read the bank file at ``path`` and check it against the pack. Raises ``BankRefused``."""
    raw_bytes = path.read_bytes()
    raw = yaml.safe_load(raw_bytes.decode("utf-8"))
    checker = _Checker(raw, vocabulary, pack)
    bank = checker.build(hashlib.sha256(raw_bytes).hexdigest())
    if checker.refusals:
        raise BankRefused(checker.refusals)
    return bank


def _slots(text: Any) -> frozenset[str] | None:
    """The slots of a pattern, or ``None`` when it is no pattern: a bare brace, or a slot with a
    conversion or a format. ``string.Formatter`` reads the pattern; no regex does."""
    if not isinstance(text, str):
        return None
    try:
        parts = list(string.Formatter().parse(text))
    except ValueError:
        return None
    if any(spec or conversion for _, _, spec, conversion in parts):
        return None
    return frozenset(name for _, name, _, _ in parts if name is not None)


class _Checker:
    def __init__(self, raw: Mapping[str, Any], vocabulary: Vocabulary, pack: Pack):
        self.raw, self.vocabulary, self.pack = raw, vocabulary, pack
        self.refusals: list[Refusal] = []

    def refuse(self, code: str, item: str, detail: str) -> None:
        self.refusals.append(Refusal(code, item, detail))

    def fields(self, item: str, part: str, body: Mapping[str, Any]) -> None:
        for name in body:
            if name not in _FIELDS[part]:
                self.refuse("unknown-field", item, f"`{name}` is no field of a {part}")

    def build(self, digest: str) -> Bank:
        raw = self.raw
        self.fields("bank", "bank", raw)
        split = raw.get("split")
        if split not in self.vocabulary.splits:
            self.refuse("undeclared-value", "bank", f"`{split}` is no declared split")
        if raw.get("pack") != self.pack.id:
            self.refuse(
                "unknown-reference", "bank", f"the bank is for `{raw.get('pack')}`, not the pack"
            )
        frames = self.frames(raw.get("frames") or {})
        forms: list[Form] = []
        seen_ids: set[str] = set()
        seen_text: dict[str, str] = {}
        for body in raw.get("forms") or ():
            form = self.form(body)
            if form is None:
                continue
            if form.id in seen_ids:
                self.refuse("duplicate-id", form.id, "a form id is used twice")
            elif form.text in seen_text:
                self.refuse(
                    "duplicate-pattern",
                    form.id,
                    f"has the text of `{seen_text[form.text]}`; one text is one form",
                )
            seen_ids.add(form.id)
            seen_text.setdefault(form.text, form.id)
            forms.append(form)
        served: dict[str, list[Form]] = {}
        for form in sorted(forms, key=lambda f: f.id):
            for key in form.serves:
                served.setdefault(key, []).append(form)
        return Bank(
            self.pack.id,
            split or "",
            digest,
            frames,
            tuple(forms),
            {key: tuple(found) for key, found in served.items()},
        )

    def frames(self, raw: Mapping[str, Any]) -> dict[str, Frame]:
        frames: dict[str, Frame] = {}
        for kind, body in raw.items():
            item = f"frame {kind}"
            self.fields(item, "frame", body)
            signed, unsigned = body.get("signed"), body.get("unsigned")
            for label, text, needs in (
                ("signed", signed, {"by", "date"}),
                ("unsigned", unsigned, {"date"}),
            ):
                slots = _slots(text)
                if slots is None or not needs <= slots <= FRAME_SLOTS:
                    self.refuse(
                        "invalid-value",
                        item,
                        f"`{label}` is a pattern of {sorted(needs)} and no other slot",
                    )
            frames[kind] = Frame(signed or "", unsigned or "")
        carried = {
            self.pack.artefact_default,
            *self.pack.artefact_kinds.values(),
            *self.pack.operation_kinds.values(),
        }
        for kind in sorted(carried - set(frames)):
            self.refuse(
                "unknown-reference",
                f"frame {kind}",
                "the pack has documents of this kind and the bank no frame",
            )
        return frames

    def form(self, body: Mapping[str, Any]) -> Form | None:
        item = f"form {body.get('id')}"
        self.fields(item, "form", body)
        slots = _slots(body.get("text"))
        if slots is None:
            self.refuse(
                "invalid-value",
                item,
                "`text` is no pattern: a bare brace, or a conversion or a format",
            )
            return None
        if not slots <= SLOTS:
            self.refuse(
                "undeclared-value",
                item,
                f"slots {sorted(slots - SLOTS)} are no slot of the renderer",
            )
        serves = tuple(body.get("serves") or ())
        if not serves:
            self.refuse("invalid-value", item, "`serves` names at least one item")
        for key in serves:
            self.serves(item, key, slots)
        probe_only = all(key.startswith("probe.") for key in serves)
        level = body.get("level")
        if probe_only and level is not None:
            self.refuse("invalid-value", item, "a form that serves only probes has no level")
        elif not probe_only and level not in LEVELS:
            self.refuse(
                "undeclared-value", item, f"`{level}` is no level that the template renderer builds"
            )
        elif level is not None and level not in self.vocabulary.levels:
            self.refuse("undeclared-value", item, f"`{level}` is no declared implicitness level")
        return Form(body.get("id", ""), serves, level, body.get("text", ""), slots)

    def serves(self, item: str, key: str, slots: frozenset[str]) -> None:
        family, _, name = str(key).partition(".")
        registry = {
            "statement": self.vocabulary.commitments,
            "rule": self.vocabulary.rule_shapes,
            "operation": self.pack.operation_kinds,
            "probe": self.vocabulary.probe_forms,
        }
        if family == "qualifier":
            kind, _, word = name.partition(".")
            known = (
                (kind == "scope" and not word)
                or (kind == "hedge" and word in self.vocabulary.hedges)
                or (kind == "basis" and word in self.vocabulary.bases)
            )
            if not known:
                self.refuse(
                    "unknown-reference",
                    item,
                    f"`{key}` is no scope, hedge or basis that the world declares",
                )
            elif _QUALIFIER_TEXT not in slots:
                self.refuse(
                    "invalid-value", item, "a qualifier holds `{text}`, the sentence that it wraps"
                )
        elif family not in FAMILIES or name not in registry.get(family, ()):
            self.refuse(
                "unknown-reference",
                item,
                f"`{key}` serves nothing that the world, the pack or the registries declare",
            )
        elif _QUALIFIER_TEXT in slots:
            self.refuse("invalid-value", item, "only a qualifier holds `{text}`")
