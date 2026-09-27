"""One conservative fold for comparing authored vocabulary terms.

``fold_term`` is the single comparison key for tags, collection claims and
routing terms. Two terms are variants of one another when their folds are
equal. The fold is a key, never display text: callers keep a real spelling
(for tags, the writers' normal form of the most-used one) and only compare
folds.

The fold applies NFKC, casefolds, maps runs of ``_``, whitespace and ``-`` to
one ``-``, strips edge hyphens, and removes one plural ``-s``/``-es``
(``-ies`` becomes ``-y``) from the final hyphen segment only. It never removes
``-ing`` or ``-ed``: ``training``/``trains``, ``recording``/``records`` and
``embedded``/``embeddings`` are different words, and a wrong merge silently
joins two concepts where a missed one only leaves two spellings apart. Words
under four letters, non-alphabetic segments and ``EXCEPTIONS`` are never
inflected.
"""

from __future__ import annotations

import functools
import re
import unicodedata

__all__ = ["EXCEPTIONS", "fold_term"]

#: Forms whose trailing ``s`` is not a plural of the stem. They fold only by
#: case and separator.
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
        # -ing and -ed words kept from the earlier fold; harmless without
        # progressive folding and they keep the set a superset.
        "building", "ceiling", "clothing", "during", "evening", "funding",
        "heading", "housing", "landing", "listing", "meeting", "morning",
        "nothing", "painting", "drawing", "setting", "something",
        "anything", "everything", "sibling", "wedding",
        "hundred", "kindred", "naked", "sacred", "wicked",
    }
)

# Hyphens join the run so a writer's per-character mapping ("x  y" -> "x--y")
# folds with the original spelling.
_SEPARATORS = re.compile(r"[\s_-]+")
_VOWELS = frozenset("aeiouy")


def _plural(word: str) -> str:
    if len(word) < 4 or not word.isalpha() or word in EXCEPTIONS:
        return word
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


@functools.lru_cache(maxsize=65536)
def _fold(text: str) -> str:
    term = unicodedata.normalize("NFKC", text).casefold()
    term = _SEPARATORS.sub("-", term).strip("-")
    if not term:
        return ""
    head, _, last = term.rpartition("-")
    folded = _plural(last)
    return f"{head}-{folded}" if head else folded


def fold_term(text: str) -> str:
    """Return the comparison key shared by every variant of ``text``."""
    return _fold(str(text))
