"""The unit kinds Exomem ships, their heading spellings and the closed kind-trait set.

The shipped kinds are data in the `kinds.yaml` pack. This leaf module reads that
pack once, so `semantic_blocks` (which parses headings) and
`semantic_language_registry` (which governs vault kinds) read one source and
neither imports the other for it. A kind trait is a promise that code keeps; the
set is closed here and each reader reads a trait through the page's registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

import yaml

from . import semantic_labels
from .vocabulary import registry as vocabulary_registry

#: The closed kind-trait set. Each member is something code does with a unit of that kind, read
#: through `SemanticLanguageRegistry.has`, `traits_of` and `kinds_with`. Adding a member is a code
#: change and a schema change, because a trait is a promise code keeps; the pack and a vault
#: assign members to kinds, and an assignment that names another trait is refused.
# nosemgrep: ep-word-set -- the closed set this schema declares and code implements, not text
KIND_TRAITS = frozenset(
    (
        "assertion",
        "page_claim",
        "procedure",
        "outcome",
        "question",
        "credence",
        "dated_event",
    )
)

def require_trait(trait: str) -> str:
    """`trait` itself. A name outside `KIND_TRAITS` is a defect in the caller, so it raises
    instead of reading false for every kind."""
    if trait not in KIND_TRAITS:
        raise ValueError(f"{trait!r} is not a kind trait")
    return trait


_PACK = "kinds.yaml"
_PACK_FIELDS = frozenset({"description", "labels", "heading_aliases", "traits", "teach"})
_COMPACT_FIELDS = frozenset({"description", "labels"})
# The shape a pack spelling already has: lower-case letters, digits and underscores.
_SPELLING_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class PackKind:
    """One shipped kind, as the pack states it."""

    key: str
    description: str
    # Each (language tag, label) pair, sorted. A label is a heading alias in that language.
    labels: tuple[tuple[str, str], ...] = ()
    heading_aliases: tuple[str, ...] = ()
    traits: frozenset[str] = frozenset()
    compact: bool = False
    # The one line bootstrap teaches for this kind; absent for a kind bootstrap does not name.
    teach: str | None = None


@lru_cache(maxsize=1)
def pack() -> dict[str, PackKind]:
    """The shipped kinds in pack order, compact kinds last. A malformed pack is a build defect."""
    return _from_pack(vocabulary_registry.pack_text(_PACK))


def _from_pack(text: str) -> dict[str, PackKind]:
    data = yaml.safe_load(text)
    kinds: dict[str, PackKind] = {}
    spellings: dict[str, str] = {}
    for section, fields, compact in (
        ("kinds", _PACK_FIELDS, False),
        ("compact_kinds", _COMPACT_FIELDS, True),
    ):
        for key, value in data[section].items():
            unknown = sorted(set(value) - fields)
            if unknown or key in kinds or not _SPELLING_RE.match(key):
                raise RuntimeError(
                    f"shipped kinds pack: {key!r} is a repeated or malformed key or has "
                    f"unknown fields {unknown}"
                )
            raw_traits = value.get("traits", [])
            if not isinstance(raw_traits, list) or any(not isinstance(t, str) for t in raw_traits):
                raise RuntimeError(f"shipped kinds pack: {key!r} traits: must be a list of strings")
            undeclared = sorted(set(raw_traits) - KIND_TRAITS)
            if undeclared:
                raise RuntimeError(
                    f"shipped kinds pack: {key!r} traits {undeclared} are not declared; "
                    f"use {sorted(KIND_TRAITS)}"
                )
            read = semantic_labels.read_labels(value.get("labels"))
            if read is None or read[1] or not read[0]:
                raise RuntimeError(
                    f"shipped kinds pack: {key!r} needs labels that map a language tag to a label"
                )
            aliases = tuple(value.get("heading_aliases", []))
            for spelling in (key, *aliases):
                if not isinstance(spelling, str) or not _SPELLING_RE.match(spelling):
                    raise RuntimeError(f"shipped kinds pack: {key!r} spelling {spelling!r} is malformed")
                if spelling in spellings and spellings[spelling] != key:
                    raise RuntimeError(
                        f"shipped kinds pack: {spelling!r} names both {spellings[spelling]!r} "
                        f"and {key!r}"
                    )
                spellings[spelling] = key
            kinds[key] = PackKind(
                key=key,
                description=str(value["description"]),
                labels=read[0],
                heading_aliases=aliases,
                traits=frozenset(raw_traits),
                compact=compact,
                teach=vocabulary_registry.pack_teach("kinds", key, value.get("teach")),
            )
    return kinds


@lru_cache(maxsize=1)
def block_kinds() -> frozenset[str]:
    """The shipped kinds a `##` heading can name."""
    return frozenset(key for key, kind in pack().items() if not kind.compact)


@lru_cache(maxsize=1)
def compact_kinds() -> frozenset[str]:
    """The shipped kinds that have no heading, such as the one-line observation."""
    return frozenset(key for key, kind in pack().items() if kind.compact)


@lru_cache(maxsize=1)
def heading_aliases() -> dict[str, str]:
    """Each shipped heading spelling other than a kind's own key, mapped to its kind."""
    return {
        alias: kind.key
        for kind in pack().values()
        if not kind.compact
        for alias in kind.heading_aliases
    }
