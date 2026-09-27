"""One conservative English fold for comparing vocabulary terms.

`fold_term` lowercases, maps `_` and whitespace runs to `-`, and folds regular
inflection on each hyphen-separated part: plural `-s`/`-es` (and `-ies` to
`-y`), and `-ing`/`-ed` on stems of four or more letters. Words under four
letters and the declared `EXCEPTIONS` are never folded. The fold is applied to
the fixpoint, so it is idempotent: a folded term that is stored and compared
again folds to itself.

It is a comparison key, never a rewrite. Authored tags, titles and claims keep
their spelling; two terms meet when their folds are equal. Being conservative
matters more than being complete: a fold that merges two different words
routes a note somewhere it does not belong, while a missed fold only leaves
two spellings apart, as they were before.
"""

from __future__ import annotations

import functools
import re

#: Words that end like an inflection but are not one. Deliberately small and
#: literal; the length floor already protects most short words.
EXCEPTIONS: frozenset[str] = frozenset(
    {
        "access",
        "address",
        "always",
        "analysis",
        "atlas",
        "basis",
        "bonus",
        "bus",
        "business",
        "campus",
        "canvas",
        "census",
        "chaos",
        "consensus",
        "corpus",
        "crisis",
        "during",
        "focus",
        "gas",
        "hundred",
        "lens",
        "news",
        "perhaps",
        "previous",
        "process",
        "series",
        "species",
        "status",
        "thesis",
        "various",
        "virus",
    }
)

_MIN_WORD = 4
_MIN_STEM = 4
_SEPARATORS = re.compile(r"[\s_]+")
_UNDOUBLE_KEEP = frozenset("lsz")


def _undouble(stem: str) -> str:
    if len(stem) > _MIN_STEM and stem[-1] == stem[-2] and stem[-1] not in _UNDOUBLE_KEEP:
        return stem[:-1]
    return stem


def _fold_once(word: str) -> str:
    if len(word) < _MIN_WORD or word in EXCEPTIONS or not word.isalpha():
        return word
    if word.endswith("ing") and len(word) - 3 >= _MIN_STEM:
        return _undouble(word[:-3])
    if word.endswith("ed") and not word.endswith("eed") and len(word) - 2 >= _MIN_STEM:
        return _undouble(word[:-2])
    if word.endswith("ies") and len(word) - 3 >= 3:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


@functools.lru_cache(maxsize=65536)
def _fold_word(word: str) -> str:
    current = word
    while True:
        folded = _fold_once(current)
        if folded == current:
            return current
        current = folded


def fold_term(text: str) -> str:
    """The comparison key of one vocabulary term. See the module docstring."""
    joined = _SEPARATORS.sub("-", str(text).strip().casefold())
    return "-".join(_fold_word(part) for part in joined.split("-"))
