"""The label registries Exomem ships, and the rules a key, a label and a language tag follow.

A label registry is a closed set of keys. Each key has a description and labels keyed by
language, and no behaviour. Each registry ships as data in its own pack, and a vault adds keys
or labels in the section of `_Schema/semantic-language-registry.yaml` that has the
registry's name. This leaf module reads each pack once, so `semantic_language_registry`
(which governs the vault's entries) and the bootstrap vocabulary (which teaches the shipped
entries) read one source. The dimension registry is the one label registry whose entries also
list members and display behaviours; those two fields change what a key groups, never what
code does with a word.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import Any

import yaml

from .vocabulary import registry as vocabulary_registry


@dataclass(frozen=True, slots=True)
class LabelRegistry:
    """One label registry. `section` names its pack file and its vault section; `noun`
    names one of its entries in a finding or an error code. `beside_probabilities` marks a
    registry whose row also takes a probability, so none of its labels may look like one.
    `has_members` marks a registry whose entries also list `members` and `display`.
    `on_pages` marks a registry whose keys a unit row writes, so a page depends on each saved
    key; a dimension key appears only in a filter, which names the keys that exist."""

    section: str
    noun: str
    beside_probabilities: bool = False
    has_members: bool = False
    on_pages: bool = True

    @property
    def entry_fields(self) -> frozenset[str]:
        """The fields one entry of this registry takes."""
        return ENTRY_FIELDS | MEMBER_FIELDS if self.has_members else ENTRY_FIELDS

    @property
    def pack_file(self) -> str:
        return f"{self.section.replace('_', '-')}.yaml"

    @property
    def entry_words(self) -> str:
        """One entry in prose, such as `belief level`."""
        return self.noun.replace("_", " ")

    @property
    def section_words(self) -> str:
        """The registry in prose, such as `belief levels`."""
        return self.section.replace("_", " ")


VERDICTS = LabelRegistry("verdicts", "verdict")
BELIEF_LEVELS = LabelRegistry("belief_levels", "belief_level", beside_probabilities=True)
MODALITIES = LabelRegistry("modalities", "modality")
INFERENCE_MODES = LabelRegistry("inference_modes", "inference_mode")
DIMENSIONS = LabelRegistry("dimensions", "dimension", has_members=True, on_pages=False)
# The label registries the language implements. Code reads each one for its own unit-metadata
# key or its own filter field, so this set is fixed by the code; the entries of each registry
# are data.
REGISTRIES = (VERDICTS, BELIEF_LEVELS, MODALITIES, INFERENCE_MODES, DIMENSIONS)

# nosemgrep: ep-word-set -- an entry's own field names, fixed by the registry schema
ENTRY_FIELDS = frozenset({"labels", "description"})
# nosemgrep: ep-word-set -- the extra field names of a dimension entry, fixed by the registry schema
MEMBER_FIELDS = frozenset({"members", "display"})
# The three lists a dimension's `members` takes: relation families, unit kinds and governed
# unit-metadata keys. The schema fixes them; each list's entries are data.
# nosemgrep: ep-word-set -- the member lists, fixed by the registry schema
MEMBER_LISTS = ("families", "kinds", "keys")
# The one wildcard a member list takes, and only `kinds` takes it: every kind in the page's
# registry, so a vault kind joins the dimension without a registry save.
ALL_KINDS = "*"
# The closed display-behaviour set. Each names something compiled context is to do within one
# dimension's group, changing only the order and annotation of the context. The schema accepts
# only these names; no reader applies one yet. Adding one is a schema change, as for a kind trait.
# nosemgrep: ep-word-set -- the closed set this schema declares for a vault to choose from, not text
DISPLAY_BEHAVIOURS = frozenset(("lead_with_current", "show_origin", "flag_open", "order_in_time"))
# The shape a key has: lower-case letters, digits and underscores.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# The shape of a label's language tag after casefolding, such as `en`, `fi` or `pt-br`: a short
# language subtag and optional subtags. It checks the shape only; any language is accepted.
LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
# The longest key or label an entry takes.
MAX_SPELLING = 64
# The probability or number shape a governed row accepts: plain decimal notation, so `nan`,
# `inf`, `1_000` and a percentage are refused. It checks a field's format, not its meaning.
_DECIMAL = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
NUMBER_RE = re.compile(rf"^{_DECIMAL}(?:[eE][+-]?\d+)?$")
PERCENT_RE = re.compile(rf"^(?P<number>{_DECIMAL})\s*%$")


def is_probability(text: str) -> bool:
    """Whether `text` is a probability as a row states one: a plain number from 0 to 1."""
    return bool(NUMBER_RE.match(text)) and 0 <= float(text) <= 1


# ZERO WIDTH NON-JOINER and ZERO WIDTH JOINER, which Unicode lets a word carry between two
# letters in scripts such as Persian or Malayalam.
# nosemgrep: ep-word-set -- two code points that the Unicode standard fixes, not words
_JOINERS = frozenset({"\u200c", "\u200d"})


def heading_label_problem(form: str) -> str | None:
    """Why `form`, a kind label in the registry's heading form, cannot name a heading; else None.

    The heading form is NFKC-normalised and casefolded, with separators folded to `_`. A
    character is valid when its Unicode general category is a letter, a mark or a number, when
    it is that `_`, or when it is a joiner between two letters, marks or numbers. The first
    character is a letter or a number. The rule reads Unicode categories, never a script list.
    """
    if not form:
        return "has no letter or number, so no heading can spell it"
    for index, char in enumerate(form):
        if _word_character(char) and (index > 0 or unicodedata.category(char)[0] != "M"):
            continue
        if char == "_" and index > 0:
            continue
        if char in _JOINERS and 0 < index < len(form) - 1 and all(
            _word_character(form[index + step]) for step in (-1, 1)
        ):
            continue
        return (
            f"holds U+{ord(char):04X}; a heading label holds only letters, marks, numbers and "
            "separators, and starts with a letter or a number"
        )
    return None


def _word_character(char: str) -> bool:
    """A letter, a mark or a number, by its Unicode general category."""
    return unicodedata.category(char)[0] in "LMN"


@dataclass(frozen=True, slots=True)
class LabelDefinition:
    """One entry: a key, its labels and what it says. No behaviour.

    `labels` holds each (language tag, label) pair, sorted, with the tag casefolded. A vault
    entry for a shipped key holds only what the vault wrote: its own labels, and a description
    only when the vault wrote one, which makes the registry ignore that entry.
    """

    key: str
    labels: tuple[tuple[str, str], ...]
    description: str
    core: bool = False
    # A dimension's member lists, as (list name, values) pairs in `MEMBER_LISTS` order, and its
    # display behaviours. Both are empty on an entry of any other registry.
    members: tuple[tuple[str, tuple[str, ...]], ...] = ()
    display: tuple[str, ...] = ()

    def members_of(self, list_name: str) -> tuple[str, ...]:
        """The values of one member list, or none when the entry does not carry it."""
        return next((values for name, values in self.members if name == list_name), ())

    @property
    def spellings(self) -> tuple[str, ...]:
        """The key and every label: each spelling a unit row may use for this entry."""
        return (self.key, *(text for _language, text in self.labels))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key, "labels": dict(self.labels), "description": self.description,
        }
        if self.members:
            out["members"] = {name: list(values) for name, values in self.members}
        if self.display:
            out["display"] = list(self.display)
        if self.core:
            out["core"] = True
        return out


def fold(raw: str) -> str:
    """A spelling as the language compares it: NFKC-normalised and casefolded."""
    return unicodedata.normalize("NFKC", raw.strip()).casefold()


def read_labels(raw: object) -> tuple[tuple[tuple[str, str], ...], list[tuple[str, str]]] | None:
    """The valid (language tag, label) pairs of one entry, and a (tag, problem) pair for each
    invalid one. None when `raw` is no mapping at all.

    The tag is casefolded, because a language tag is case-insensitive: `pt-BR` and `pt-br`
    are one language. The pack loader and the vault parser both read labels through here.
    """
    # A row matches a label as whole folded text, so a verdict label may hold punctuation; a
    # kind label becomes a heading key, so its registry also applies `heading_label_problem`.
    if not isinstance(raw, dict):
        return None
    labels: dict[str, str] = {}
    problems: list[tuple[str, str]] = []
    for language, text in sorted(raw.items(), key=lambda item: str(item[0])):
        problem = _label_problem(language, text)
        tag = language.casefold() if isinstance(language, str) else str(language)
        if problem is None and tag in labels:
            problem = f"{language!r} repeats the language {tag}"
        if problem is not None:
            problems.append((str(language), problem))
            continue
        labels[tag] = text.strip()
    return tuple(sorted(labels.items())), problems


def read_members(
    raw: object,
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], list[tuple[str, str]]] | None:
    """The valid member lists of one dimension, and a (list name, problem) pair for each invalid
    one. None when `raw` is no mapping at all.

    This checks shape only: that each list is a list of well-formed keys and that only `kinds`
    takes the wildcard. Whether a name exists as a family, kind or key is the registry's check,
    because it needs the page's registries. The pack loader and the vault parser both read
    members through here.
    """
    if not isinstance(raw, dict):
        return None
    lists: dict[str, tuple[str, ...]] = {}
    problems: list[tuple[str, str]] = []
    for name, values in raw.items():
        if name not in MEMBER_LISTS:
            problems.append(
                (str(name), f"is not a member list; use {', '.join(MEMBER_LISTS)}")
            )
            continue
        bad = (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) for value in values)
            or any(
                value != ALL_KINDS and not KEY_RE.match(value)
                for value in values
                if isinstance(value, str)
            )
        )
        if bad:
            problems.append(
                (name, "must be a non-empty list of keys: lower-case letters, digits and underscores")
            )
        elif name != "kinds" and ALL_KINDS in values:
            problems.append((name, f"takes no {ALL_KINDS!r}; only `kinds` does"))
        else:
            lists[name] = tuple(dict.fromkeys(values))
    return tuple((name, lists[name]) for name in MEMBER_LISTS if name in lists), problems


def read_display(raw: object) -> tuple[tuple[str, ...], list[str]] | None:
    """The declared display behaviours of one dimension in order, and each value that the closed
    set does not declare. None when `raw` is no list of strings."""
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        return None
    declared = tuple(dict.fromkeys(value for value in raw if value in DISPLAY_BEHAVIOURS))
    return declared, [value for value in dict.fromkeys(raw) if value not in DISPLAY_BEHAVIOURS]


def _label_problem(language: object, text: object) -> str | None:
    """Why `language: text` is no valid label entry, or None when it is one."""
    if isinstance(language, bool):
        # YAML reads an unquoted `no`, `on`, `off`, `yes`, `true` or `false` as a boolean.
        return (
            f"YAML read this language tag as the boolean {language}; quote the tag, such as "
            "\"no\": for Norwegian"
        )
    if not isinstance(language, str) or not LANGUAGE_TAG_RE.match(language.casefold()):
        return f"{language!r} is not a language tag such as en, fi or pt-br"
    if not isinstance(text, str) or "\n" in text or not 0 < len(text.strip()) <= MAX_SPELLING:
        return (
            f"the {language} label must be one non-empty line of at most {MAX_SPELLING} characters"
        )
    return None


@cache
def pack(registry: LabelRegistry) -> dict[str, LabelDefinition]:
    """The shipped entries of `registry` in pack order. A malformed pack is a build defect."""
    return _from_pack(registry, vocabulary_registry.pack_text(registry.pack_file))


def _from_pack(registry: LabelRegistry, text: str) -> dict[str, LabelDefinition]:
    data = yaml.safe_load(text)
    where = f"shipped {registry.section_words} pack"
    entries: dict[str, LabelDefinition] = {}
    spellings: dict[str, str] = {}
    required = (ENTRY_FIELDS | {"members"}) if registry.has_members else ENTRY_FIELDS
    for key, value in data[registry.section].items():
        unknown = sorted(set(value) - registry.entry_fields)
        if unknown or key in entries or not KEY_RE.match(key) or not required <= set(value):
            raise RuntimeError(
                f"{where}: {key!r} is a repeated or malformed key, lacks labels, a "
                f"description or members, or has unknown fields {unknown}"
            )
        read = read_labels(value["labels"])
        if read is None or read[1] or not read[0]:
            raise RuntimeError(f"{where}: {key!r} needs labels that map a language tag to a label")
        members, display = _pack_members(where, key, value) if registry.has_members else ((), ())
        entry = LabelDefinition(
            key=key, labels=read[0], description=str(value["description"]), core=True,
            members=members, display=display,
        )
        for spelling in {fold(spelling) for spelling in entry.spellings}:
            if spellings.setdefault(spelling, key) != key:
                raise RuntimeError(
                    f"{where}: {spelling!r} names both {spellings[spelling]!r} and {key!r}"
                )
        entries[key] = entry
    return entries


def _pack_members(
    where: str, key: str, value: dict[str, Any]
) -> tuple[tuple[tuple[str, tuple[str, ...]], ...], tuple[str, ...]]:
    """The members and display behaviours of one shipped dimension. A malformed pack is a build
    defect, so every problem raises."""
    members = read_members(value["members"])
    if members is None or members[1] or not members[0]:
        raise RuntimeError(f"{where}: {key!r} needs members with valid lists: {members}")
    display = read_display(value.get("display", []))
    if display is None or display[1]:
        raise RuntimeError(
            f"{where}: {key!r} display must name only {sorted(DISPLAY_BEHAVIOURS)}"
        )
    return members[0], display[0]
