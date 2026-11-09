"""The verdicts Exomem ships and the one rule that folds a spelling.

The shipped verdicts are data in the `verdicts.yaml` pack. This leaf module reads that pack
once, so `semantic_language_registry` (which governs vault verdicts) and the bootstrap
vocabulary (which teaches only the shipped ones) read one source.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

import yaml

from .vocabulary import registry as vocabulary_registry

_PACK = "verdicts.yaml"
_PACK_FIELDS = frozenset({"label", "description"})  # nosemgrep: ep-word-set -- the pack's own field names, fixed by its schema
# The shape a verdict key has: lower-case letters, digits and underscores.
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class PackVerdict:
    """One shipped verdict, as the pack states it."""

    key: str
    label: str
    description: str


def fold(raw: str) -> str:
    """A verdict spelling as the language compares it: NFKC-normalised and casefolded."""
    return unicodedata.normalize("NFKC", raw.strip()).casefold()


@lru_cache(maxsize=1)
def pack() -> dict[str, PackVerdict]:
    """The shipped verdicts in pack order. A malformed pack is a build defect."""
    return _from_pack(vocabulary_registry.pack_text(_PACK))


def _from_pack(text: str) -> dict[str, PackVerdict]:
    data = yaml.safe_load(text)
    verdicts: dict[str, PackVerdict] = {}
    spellings: dict[str, str] = {}
    for key, value in data["verdicts"].items():
        unknown = sorted(set(value) - _PACK_FIELDS)
        if unknown or key in verdicts or not KEY_RE.match(key) or set(value) != _PACK_FIELDS:
            raise RuntimeError(
                f"shipped verdicts pack: {key!r} is a repeated or malformed key, lacks a label "
                f"or description, or has unknown fields {unknown}"
            )
        verdict = PackVerdict(key=key, label=str(value["label"]), description=str(value["description"]))
        for spelling in (fold(key), fold(verdict.label)):
            if spellings.setdefault(spelling, key) != key:
                raise RuntimeError(
                    f"shipped verdicts pack: {spelling!r} names both {spellings[spelling]!r} and {key!r}"
                )
        verdicts[key] = verdict
    return verdicts
