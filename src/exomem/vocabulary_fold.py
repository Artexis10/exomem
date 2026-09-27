"""One conservative fold for comparing authored vocabulary terms.

``fold_term`` is the single comparison key for tags, collection claims and
routing terms. Two terms are variants of one another when their folds are
equal. The fold is a key, never display text: callers keep whichever real
variant they choose (for tags, the most-used one) and only compare folds.

The fold lowercases, maps ``_`` and whitespace runs to ``-``, and removes one
English inflection from the final hyphen segment: a plural ``-s``/``-es``, then
an ``-ing``/``-ed`` whose stem keeps four or more letters. Words under four
letters, non-alphabetic segments and ``EXCEPTIONS`` are never inflected, so a
fold never changes meaning at the cost of sometimes missing a variant.
"""

from __future__ import annotations

import functools
import re

__all__ = ["EXCEPTIONS", "fold_term"]

#: Forms whose apparent inflection carries meaning, or whose stem is a
#: different word. They fold only by case and separator.
EXCEPTIONS = frozenset(
    {
        # -s forms that are not plurals of the stem
        "access", "address", "alias", "always", "analysis", "arms", "atlas", "basis",
        "bias", "business", "bus", "campus", "canvas", "chaos", "corpus",
        "crisis", "customs", "diagnosis", "earnings", "gas", "glasses",
        "goods", "https", "kudos", "lens", "means", "news", "perhaps", "process",
        "proceedings", "rails", "savings", "series", "species", "status",
        "surroundings", "synopsis", "thesis", "windows",
        # -ics fields of study, not plurals of an adjective
        "analytics", "diagnostics", "dynamics", "economics", "electronics",
        "ethics", "genetics", "graphics", "heuristics", "linguistics",
        "logistics", "mathematics", "physics", "politics", "robotics",
        "semantics", "statistics",
        # -ing nouns whose stem is a different word
        "building", "ceiling", "clothing", "during", "evening", "funding",
        "heading", "housing", "landing", "listing", "meeting", "morning",
        "nothing", "painting", "drawing", "setting", "something",
        "anything", "everything", "sibling", "wedding",
        # -ed words that are not past forms
        "hundred", "kindred", "naked", "sacred", "wicked",
    }
)

_SEPARATORS = re.compile(r"[\s_]+")
_VOWELS = frozenset("aeiouy")
# Doubled final consonants that belong to the stem (fall, pass, stuff, buzz).
_KEPT_DOUBLES = frozenset("lsfz")


def _plural(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("es"):
        base = word[:-2]
        if base.endswith(("ss", "us", "sh", "x", "zz")):
            return base
        if base.endswith("ch"):
            # match-es / coach-es drop "es"; cache-s / niche-s drop only "s".
            before = base[-3:-2]
            single_vowel = before in _VOWELS and base[-4:-3] not in _VOWELS
            return word[:-1] if single_vowel else base
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _verbal(word: str) -> str:
    if word.endswith("eed"):
        return word
    for suffix in ("ing", "ed"):
        if not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        if len(stem) < 4 or not _VOWELS.intersection(stem):
            return word
        if len(stem) >= 5 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS | _KEPT_DOUBLES:
            stem = stem[:-1]
        return stem
    return word


@functools.lru_cache(maxsize=65536)
def _fold_word(word: str) -> str:
    for _ in range(3):
        if len(word) < 4 or not word.isalpha() or word in EXCEPTIONS:
            return word
        folded = _plural(word)
        if folded not in EXCEPTIONS:
            folded = _verbal(folded)
        if folded == word:
            return word
        word = folded
    return word


def fold_term(text: str) -> str:
    """Return the comparison key shared by every variant of ``text``."""
    term = _SEPARATORS.sub("-", str(text).strip().lower()).strip("-")
    if not term:
        return ""
    head, _, last = term.rpartition("-")
    folded = _fold_word(last)
    return f"{head}-{folded}" if head else folded
