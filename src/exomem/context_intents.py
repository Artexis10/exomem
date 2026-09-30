"""Intent shapes a context role can require instead of a bare cue substring.

A role cue is a substring of the turn: fine for a hint like "prefer", wrong for
a role that serves sensitive units. The `contact` role therefore matches an
INTENT over the turn analysis's own tokens (the tokenizer activation already
uses), never a raw substring:

* a contact noun tied to the person: a possessive of a resolved person (or of
  any name when none is known) or a possessive pronoun before the noun ("Ana's
  phone", "their address"), or a whole contact phrase ("phone number", "mailing
  address", "contact details"); or
* a reach verb whose object is the person: a resolved person's name or a
  pronoun ("email Ana about the class", "how do I reach her").

Whole tokens only, so "phonetic", "numbered", "the email thread", "call it done",
"address this issue" and "contact lens" select nothing. The packet exists only
around a resolved anchor, so a whole contact phrase is tied to that anchor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from .working_set_index import STOPWORDS, fold_possessive

CONTACT_NOUNS = frozenset({"phone", "number", "email", "e-mail", "address", "contact"})
CONTACT_PHRASES = (
    ("phone", "number"),
    ("cell", "number"),
    ("mobile", "number"),
    ("email", "address"),
    ("e-mail", "address"),
    ("mailing", "address"),
    ("home", "address"),
    ("contact", "details"),
    ("contact", "info"),
    ("contact", "information"),
)
REACH_VERBS = frozenset(
    {
        "call", "calling", "phone", "phoning", "email", "emailing", "e-mail", "text",
        "texting", "message", "messaging", "reach", "reaching", "contact", "contacting",
    }
)
#: "her contact lens" is a thing, not how to reach her.
NOT_A_PERSON_NOUN = frozenset({"lens", "lenses"})
POSSESSIVE_PRONOUNS = frozenset({"their", "her", "his"})
PRONOUN_OBJECTS = frozenset({"him", "her", "them"})
#: The intent names a role may declare.
INTENTS = frozenset({"contact"})
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def anchor_terms(titles: Iterable[str]) -> frozenset[str]:
    """Casefolded name tokens of the resolved anchors' titles."""
    terms: set[str] = set()
    for title in titles:
        terms.update(fold_possessive(token) for token in _WORD.findall(str(title).casefold()))
    return frozenset(terms - STOPWORDS)


def _possessive_of(token: str, names: frozenset[str]) -> bool:
    stem = fold_possessive(token)
    if stem == token or stem in STOPWORDS:
        return False
    return not names or stem in names


def contact_intent(tokens: Sequence[str], names: frozenset[str] = frozenset()) -> bool:
    """Whether the turn's tokens ask to reach, or for the contact details of, a person."""
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if (token, following) in CONTACT_PHRASES:
            return True
        if token in CONTACT_NOUNS and index > 0 and following not in NOT_A_PERSON_NOUN:
            before = tokens[index - 1]
            if before in POSSESSIVE_PRONOUNS or _possessive_of(before, names):
                return True
        if token in REACH_VERBS and following:
            obj = following
            if obj in PRONOUN_OBJECTS or fold_possessive(obj) in names:
                return True
    return False


def intent_matches(intent: str, analysis: Any, names: frozenset[str] = frozenset()) -> bool:
    """Dispatch a role's declared `intent` over the turn analysis's tokens."""
    if intent == "contact":
        return contact_intent(tuple(getattr(analysis, "tokens", ()) or ()), names)
    return False
