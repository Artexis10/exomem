"""The label registries Exomem ships, and the rules a key, a label and a language tag follow.

A label registry is a closed set of keys. Each key has a description and labels keyed by
language, and no behaviour. Each registry ships as data in its own pack, and a vault adds keys
or labels in the section of `_Schema/semantic-language-registry.yaml` that has the
registry's name. This leaf module reads each pack once, so `semantic_language_registry`
(which governs the vault's entries) and the bootstrap vocabulary (which teaches the shipped
entries) read one source.
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
    names one of its entries in a finding or an error code."""

    section: str
    noun: str

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
BELIEF_LEVELS = LabelRegistry("belief_levels", "belief_level")
MODALITIES = LabelRegistry("modalities", "modality")
INFERENCE_MODES = LabelRegistry("inference_modes", "inference_mode")
# The label registries the language implements. Code reads each one for its own unit-metadata
# key, so this set is fixed by the code; the entries of each registry are data.
REGISTRIES = (VERDICTS, BELIEF_LEVELS, MODALITIES, INFERENCE_MODES)

# nosemgrep: ep-word-set -- an entry's own field names, fixed by the registry schema
ENTRY_FIELDS = frozenset({"labels", "description"})
# The shape a key has: lower-case letters, digits and underscores.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# The shape of a label's language tag after casefolding, such as `en`, `fi` or `pt-br`: a short
# language subtag and optional subtags. It checks the shape only; any language is accepted.
LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")
# The longest key or label an entry takes.
MAX_SPELLING = 64


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

    @property
    def spellings(self) -> tuple[str, ...]:
        """The key and every label: each spelling a unit row may use for this entry."""
        return (self.key, *(text for _language, text in self.labels))

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key, "labels": dict(self.labels), "description": self.description,
        }
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
    for key, value in data[registry.section].items():
        unknown = sorted(set(value) - ENTRY_FIELDS)
        if unknown or key in entries or not KEY_RE.match(key) or set(value) != ENTRY_FIELDS:
            raise RuntimeError(
                f"{where}: {key!r} is a repeated or malformed key, lacks labels or a "
                f"description, or has unknown fields {unknown}"
            )
        read = read_labels(value["labels"])
        if read is None or read[1] or not read[0]:
            raise RuntimeError(f"{where}: {key!r} needs labels that map a language tag to a label")
        entry = LabelDefinition(
            key=key, labels=read[0], description=str(value["description"]), core=True
        )
        for spelling in {fold(spelling) for spelling in entry.spellings}:
            if spellings.setdefault(spelling, key) != key:
                raise RuntimeError(
                    f"{where}: {spelling!r} names both {spellings[spelling]!r} and {key!r}"
                )
        entries[key] = entry
    return entries
