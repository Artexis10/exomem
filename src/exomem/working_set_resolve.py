"""Deterministic anchor resolution for the context compiler.

The rule is the referents rule, generalised past people. A turn reaches an
anchor through CATEGORICAL evidence kinds — never a score — and the operation
abstains when nothing resolves rather than guessing. Disambiguation between two
equally-resolved senses is the agent's job, not the server's: the constitution
forbids a server-side reasoning model, so an ambiguous turn is reported with
both anchors and no lane is run for either.

Pure logic on purpose. Everything this module needs arrives as facts — anchor
rows, retrieval paths, routing targets, an optional vector map — so the rule can
be tested without a vault, a sidecar, or a model, and so the same rule is
exercised by the unit tests and by the live operation.
"""

from __future__ import annotations

import functools
import math
import re
import statistics
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, NamedTuple

from .ranking_config import DEFAULT_RANKING, RankingConfig
from .text_scripts import JAPANESE_PARTICLES, is_hiragana
from . import working_set_anaphora
from .working_set_index import (
    RARE_TERM_MAX_ANCHORS,
    STOPWORDS,
    derived_short_name,
    fold_plural,
    fold_possessive,
    normalize,
    tokens_of,
)

#: The closed evidence vocabulary. Order is the reporting order.
EVIDENCE_KINDS: tuple[str, ...] = (
    "exact_alias",
    "lexical_overlap",
    "rare_term",
    "vector_band",
    "category_match",
    "claims_match",
    "retrieval",
    "graph_corroboration",
    "usage_prior",
    "recency",
    "continuity",
    "conversation",
    "agent_choice",
)

#: `RARE_TERM_MAX_ANCHORS` lives in `working_set_index` (re-exported here):
#: `_finalize_anchor_aliases`'s derived-short-name rarity gate needs it too,
#: and that module has no dependency on this one.

#: The shortest term that may be a LEAD to an anchor. Rarity among anchor
#: NAMES cannot tell a genuinely short name from an everyday two-letter word
#: that happens to appear in a title: "go" is no stopword, it names few
#: anchors in any small catalogue, and an ordinary "so should I go with the
#: cheaper one?" therefore reached a page titled "... Go ..." on that one word.
#: Length is the discriminator a counting table has no way to supply. Three
#: is the floor because real short names start there ("hob", "van", "PR");
#: below it a shared term is coincidence, not reference.
#:
#: Applied only to a term written entirely in ASCII LETTERS. The reasoning
#: above is about an alphabet where an ordinary word is several letters
#: long; two characters is an ordinary word in CJK — a city, a company, a
#: person — and counting code points there turns a real name into a
#: non-name. A term that is not all-ASCII keeps whatever rarity its
#: document count earns it.
#:
#: And not to a two-letter ACRONYM both sides spell as one: the turn writes
#: it as exactly two capitals ("AI", "UI", "EU") AND the anchor's own title
#: writes it in capitals too ("AI Subscriptions"). Either half alone is not
#: enough. Capitals in a turn are also emphasis ("should I GO with..."), a
#: grade ("I got a C"), or a dotted abbreviation ("U.S."), and each of those
#: reached an unrelated anchor and served it when the turn's casing sufficed;
#: an anchor whose title writes the word "Go" is not named by an emphatic
#: "GO". A single capital never qualifies — a one-letter name is reached by
#: its own spelling (`exact_alias`), never as a lead — and a turn with no
#: lower-case letter at all carries no casing signal and is read as lower
#: case (`TurnAnalysis.acronyms`).
RARE_TERM_MIN_CHARS = 3

#: Exactly two ASCII capitals, found on the RAW text because `normalize()`
#: casefolds. Bounded on both sides by anything that is not a letter, a digit
#: or a dot, so "AI's" and "AI-driven" still show "AI" while "HTTPS" and a
#: dotted "U.S." show nothing.
_ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9.])[A-Z]{2}(?![A-Za-z0-9]|\.[A-Za-z])")

#: `usage_prior` is a tie-break only. It never contributes to the two-kinds
#: rule, because "you looked at this a lot" is not evidence that this turn is
#: about it — that is how a rich-get-richer prior turns into a wrong anchor.
TIE_BREAK_KINDS: frozenset[str] = frozenset({"usage_prior"})

#: The prior class, and its one member. `recency` says "this anchor is at the
#: top of the hot profile" — the previous packet's own answer, the freshest
#: edit, the most-read page — and design §8's narrow amendment lets exactly
#: that fact supply the REFERENT of a turn that names nothing at all.
#:
#: Deliberately in none of the other classes. Not a tie-break, because it is
#: not ordering anything: on a referential turn it is the whole reason an
#: anchor is a candidate. Not a worded or a retrieved contact kind, because
#: the turn's words never reached this anchor and no ranking engine put it
#: there. Not in `CONTACT_KINDS`, which is what keeps `_status_for`'s four
#: existing clauses exactly as they were: the third resolves `rare_term` plus
#: any other CONTACT kind, and admitting a prior there would let one shared
#: word plus a hot page resolve an anchor nobody named. It is stripped from
#: the soundness rule's `deciding` set for the same reason, so it can never be
#: the second kind that promotes somebody else.
PRIOR_CONTACT_KINDS: frozenset[str] = frozenset({"recency"})

#: The two kinds that say "an earlier packet, or an earlier turn, already named
#: this subject". Each resolves an anchor the current turn reached by one contact
#: kind, never alone and never with qualifiers only, and the two together count
#: once (`_status_for_evidence` reads the set, not a count).
NAMED_BEFORE_KINDS: frozenset[str] = frozenset({"continuity", "conversation"})

#: Kinds that resolve an anchor by themselves. `exact_alias` because the turn
#: spelled the anchor's own name; `agent_choice` because the agent IS the
#: decider of an ambiguous turn and the server has nothing to add to a decision
#: already taken. Every other kind needs a second one.
DECIDING_ALONE_KINDS: frozenset[str] = frozenset({"exact_alias", "agent_choice"})

#: The status of a page a packet was CARRIED on (design D3), never one this
#: module produces. `resolve()` cannot return it and `_status_for` has no
#: clause for it: retrieval alone still never resolves an anchor. It is the
#: spelling a packet uses to say "no anchor was named; recall alone put this
#: page here", and it is deliberately not `resolved`, so everything keyed on
#: that word — `mint_continuity`'s ref list above all — declines it without
#: needing to know this feature exists.
RETRIEVAL_CARRIED_STATUS = "retrieval_carried"

#: The status of a page a turn NAMED but that carries nothing, because the
#: turn named another one too. Deliberately not `retrieval_carried`, which
#: says "this page carries the packet", and deliberately not reported as
#: `ambiguous`, which says two anchors RESOLVED and the agent must choose
#: between senses. This is neither: nothing resolved, nothing was carried,
#: and here are the pages the turn's own words reached, so the client can
#: ask for one by name instead of being handed an empty packet.
RETRIEVAL_NAMED_STATUS = "retrieval_named"

ANCHOR_STATUSES: tuple[str, ...] = (
    "resolved",
    "partial",
    "unresolved",
    RETRIEVAL_CARRIED_STATUS,
    RETRIEVAL_NAMED_STATUS,
)
TURN_STATUSES: tuple[str, ...] = ("resolved", "ambiguous", "unresolved")

MAX_CANDIDATES = 24
MAX_ANCHORS = 6
MAX_NGRAM = 4

#: An evidence cue must be at least this many characters long
#: (`make-activation-conventions-vault-owned` decision 1) -- the bound a
#: bare `?` fails, which is the one intended shipped difference from the
#: deleted `CUE_PATTERNS` table. `context_roles.MIN_EVIDENCE_CUE_CHARS`
#: applies the SAME bound at override-merge time (to decide whether a cue
#: the owner just added can ever be evidence, for the finding it reports);
#: this module has no dependency on `context_roles`, so the value is
#: restated here rather than imported.
MIN_EVIDENCE_CUE_CHARS = 3

@dataclass(frozen=True, slots=True)
class ReferentialVocabulary:
    """The words that make a turn point back at what the session was doing
    (close-memory-loop D2), as the vault's activation conventions define them.

    No word list lives in this module: the shipped seed is the scaffold's
    `activation-conventions.yaml` (`referential.cues`/`referential.filler`),
    extended or narrowed by the vault's override, and `analyze_turn` is handed
    the effective vocabulary by its caller.

    A cue is NECESSARY for a turn to be referential but not sufficient: the
    turn must also say nothing else besides stopwords and `filler`, because
    every cue word has an ordinary sense — "update my resume", "check the
    status of my flight", "continue the story" — and a prior must never
    answer one of those. Cues are matched on whole tokens, never as a
    substring, so "discontinue" and "statuses" are not cues.
    """

    cues: tuple[str, ...]
    filler: frozenset[str]
    #: The cues as token runs, spelled the way `tokens_of` spells a turn,
    #: longest first so an overlapping pair ("same as before", "as before")
    #: is removed as the longer one.
    phrases: tuple[str, ...]

    @classmethod
    def of(cls, cues: Iterable[str], filler: Iterable[str]) -> ReferentialVocabulary:
        cue_tuple = tuple(cues)
        phrases = tuple(
            sorted(
                {
                    phrase
                    for cue in cue_tuple
                    if (phrase := " ".join(tokens_of(normalize(cue))))
                },
                key=lambda phrase: (-len(phrase), phrase),
            )
        )
        return cls(cues=cue_tuple, filler=frozenset(filler), phrases=phrases)


def shipped_vocabulary() -> ReferentialVocabulary:
    """The shipped seed, for a caller with no vault (unit tests, benchmarks)."""
    from . import activation_conventions

    return activation_conventions.shipped_conventions().conventions.referential


#: Worded contact: the turn's OWN WORDS reached the anchor's own names, terms
#: or claims. Two of these together (or one plus any other kind besides
#: `usage_prior`) is independent evidence that the turn is about the anchor —
#: words and a ranking engine agreeing is two facts. `rare_term` is the weak
#: member: a single shared term rare enough to be a lead, never a decision by
#: itself (see the three-clause rule in `_status_for`).
WORDED_CONTACT_KINDS: frozenset[str] = frozenset(
    {"exact_alias", "lexical_overlap", "claims_match", "rare_term"}
)

#: Retrieved contact: a RANKING ENGINE surfaced the anchor near the turn.
#: `retrieval` and `vector_band` both restate "recall/vectors placed this
#: nearby" — a ranking engine agreeing with itself is one fact, however many
#: of these co-occur, and neither ever creates `graph_corroboration` on its
#: own account either (see `add_graph_corroboration`).
RETRIEVED_CONTACT_KINDS: frozenset[str] = frozenset({"retrieval", "vector_band"})

#: Kinds that establish CONTACT between a turn and an anchor — the turn actually
#: named it, claimed it, or retrieved it. `category_match`, `usage_prior` and
#: `continuity` are qualifiers: they say something about an anchor already in
#: contact, never that a turn is about one. Without this split, "how much is
#: left?" matched the cue category `fact`, every page with a `## Summary` section
#: carries `fact`, and the whole vault became a candidate on one cue.
CONTACT_KINDS: frozenset[str] = WORDED_CONTACT_KINDS | RETRIEVED_CONTACT_KINDS

#: Function words are dropped before the lexical band is measured. "the" shared
#: between a turn and a title is not a reference; two content words are.
#: `STOPWORDS` lives in `working_set_index` (re-exported here): the derived-
#: short-name validity check (`derived_short_name`) needs the SAME list, and
#: that module has no dependency on this one.
_STOPWORDS: frozenset[str] = STOPWORDS

@dataclass(frozen=True, slots=True)
class TurnAnalysis:
    """One normalised turn. Computed once and reused by every lane.

    Registry-free by design (`make-activation-conventions-vault-owned`,
    decision 1): the deleted `CUE_PATTERNS`/`_CUE_CATEGORIES` tables lived
    here, but a role's cue vocabulary is now the vault's own
    `context_roles` registry, so this dataclass no longer carries a `cues`
    field at all -- `eligible_categories()` below reads the roles and this
    analysis's `tokens` directly, without either one needing to know about
    the other's shape.
    """

    text: str
    tokens: tuple[str, ...]
    ngrams: tuple[str, ...]
    #: Does this turn point at recent work instead of naming anything? A
    #: property of the TURN alone — whether anything hot exists to point at,
    #: and whether the turn's words reached an anchor after all, are facts
    #: about the vault and the candidate set, decided in `resolve()`.
    referential: bool = False
    #: Did the turn speak a referential cue at all, whatever else it said? A
    #: cue with residue is recorded here and leaves `referential` false.
    referential_cue: bool = False
    #: The two-capital words the turn spelled as acronyms, casefolded like
    #: `tokens`: the casing the analysis otherwise discards, kept only because
    #: the rare-term length floor needs it (`RARE_TERM_MIN_CHARS`). Empty for
    #: a turn with no lower-case letter, where capitals carry no signal.
    acronyms: frozenset[str] = frozenset()
    #: Is this a short follow-up that points into the conversation's own
    #: recent answer ("what about the second one?", "and the results?")?
    #: Broader than `referential` and deliberately weaker: it never lets a
    #: prior RESOLVE anything, and is read only to carry the caller's own
    #: thread as a `partial` anchor (`is_follow_up`).
    follow_up: bool = False
    #: Words a turn token holds without a space around them (`embedded_words`):
    #: a Latin word glued to a Japanese phrase, and the parts of a Japanese
    #: run between its hiragana. Compared like tokens; they hold no position.
    words: tuple[str, ...] = ()
    #: Words the raw turn capitalises somewhere other than at a sentence start,
    #: folded like the lexical terms. Casing is how a cased script marks a word
    #: as a name; a capital that only ever opens a sentence marks nothing.
    #: Empty for a turn with no lower-case letter, where capitals carry no
    #: signal.
    capitalised: frozenset[str] = frozenset()
    #: Token indices where the raw turn punctuates a clause boundary (sentence
    #: end, comma, colon, semicolon, dash) just before that token: no run of
    #: name words spans one (`_name_span`). `tokens_of` drops punctuation, so
    #: the positions are derived from the raw text and `tokens` is unchanged.
    run_breaks: frozenset[int] = frozenset()
    #: Does the raw turn's casing say anything? True only for a turn that mixes
    #: capitalised and lower-case words: an all-lower-case turn, an all-caps
    #: one and a headline with every word capitalised carry no signal.
    cased_turn: bool = False
    #: Words the raw turn writes with a capital anywhere, a sentence start
    #: included, folded like the lexical terms. Empty when `cased_turn` is false.
    capitalised_anywhere: frozenset[str] = frozenset()
    #: Does this turn point back (`working_set_anaphora.points_back`): a
    #: pronoun or possessive, a demonstrative, a follow-up marker, an ordinal
    #: plus "one"/"option", a governed pointer or a referential cue?
    points_back: bool = False
    #: The turn's content words (`working_set_anaphora.content_words`). The
    #: conversation carry runs only when every one occurs in earlier turns.
    content_words: tuple[str, ...] = ()
    #: `points_back` with no content word at all: the verdict without a
    #: conversation to compare against.
    anaphoric: bool = False


#: A follow-up is short: at most this many tokens. "what about the second
#: one" is five; a turn long enough to say what it is about names it.
FOLLOW_UP_MAX_TOKENS = 8
#: Words that point back into the conversation instead of naming anything,
#: beyond the vault's own referential filler ("it", "that", "this", ...), which
#: `is_follow_up` reads as pointing words too: the filler is vault data, so it
#: is never restated here. Evidence of a follow-up only in a turn this short.
FOLLOW_UP_MARKERS: frozenset[str] = frozenset(
    {
        "those", "these", "they", "them", "same", "one", "ones", "first",
        "second", "third", "previous", "former", "latter", "above", "earlier",
        "other", "another", "next",
    }
)
#: Openers that continue the previous answer: "and the results?",
#: "what about ...", "how about ...".
FOLLOW_UP_OPENERS: tuple[tuple[str, ...], ...] = (
    ("and",),
    ("also",),
    ("then",),
    ("what", "about"),
    ("how", "about"),
)


#: The most words a follow-up may say besides function words, markers and
#: filler: "and the results?" says one. A turn saying more is about something.
FOLLOW_UP_MAX_CONTENT = 1


def is_follow_up(
    tokens: Sequence[str],
    *,
    referential_cue: bool = False,
    filler: frozenset[str] = frozenset(),
) -> bool:
    """A short turn that continues the conversation's own last answer.

    Short (`FOLLOW_UP_MAX_TOKENS`), saying at most `FOLLOW_UP_MAX_CONTENT`
    words of its own, and it either speaks a referential cue, opens with a
    continuing word (`FOLLOW_UP_OPENERS`), or points with a deictic or
    anaphoric word (`FOLLOW_UP_MARKERS`, or the vault's referential filler).
    "what about the quarterly budget?" opens like one and is not: it names a
    subject. Whether the turn reached
    an anchor after all is the resolver's answer, not this function's: the
    carry it enables runs only for a turn that reached none."""
    if not tokens or len(tokens) > FOLLOW_UP_MAX_TOKENS:
        return False
    words = tuple(tokens)
    opener = next(
        (opener for opener in FOLLOW_UP_OPENERS if words[: len(opener)] == opener), ()
    )
    content = [
        token
        for token in words[len(opener) :]
        if token not in _STOPWORDS and token not in FOLLOW_UP_MARKERS and token not in filler
    ]
    if len(content) > FOLLOW_UP_MAX_CONTENT:
        return False
    return bool(
        referential_cue
        or opener
        or any(token in FOLLOW_UP_MARKERS or token in filler for token in words)
    )


def is_anaphoric(
    tokens: Sequence[str],
    *,
    referential_cue: bool = False,
    vocabulary: ReferentialVocabulary | None = None,
) -> bool:
    """Taken on its own, does the turn lean on something said before? It must
    point back and bring no content word of its own (`working_set_anaphora`).
    With a conversation, the carry asks instead whether its content words all
    occur in the earlier turns (`working_set_conversation.may_carry`)."""
    return working_set_anaphora.points_back(
        tokens, referential_cue=referential_cue, follow_up_markers=FOLLOW_UP_MARKERS
    ) and not working_set_anaphora.content_words(tokens, vocabulary=_vocabulary_words(vocabulary))


def _vocabulary_words(vocabulary: ReferentialVocabulary | None) -> frozenset[str]:
    """The vault's referential cue and filler words, which are never content."""
    return _words_of_vocabulary(vocabulary if vocabulary is not None else shipped_vocabulary())


@functools.lru_cache(maxsize=16)
def _words_of_vocabulary(vocabulary: ReferentialVocabulary) -> frozenset[str]:
    return frozenset(word for phrase in vocabulary.phrases for word in phrase.split()) | vocabulary.filler


@dataclass(frozen=True, slots=True)
class AnchorFacts:
    """One index row as the resolver sees it — no sqlite types, no scores."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()
    #: An entity page's own `entity_type` (`person`, `organization`, ...),
    #: casefolded; empty for every other kind and for an entity that declares
    #: none.
    entity_type: str = ""


@dataclass(frozen=True, slots=True)
class CandidateFacts:
    """An anchor plus the categorical evidence this turn produced for it."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    evidence: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()
    #: The turn phrases (the `names & phrases` intersection) that earned this
    #: candidate's `exact_alias`, if any. Never serialised into a packet --
    #: `resolve`'s R1 same-kind subsumption rule is the only reader (fix/
    #: activation-competing-senses): a shorter spelled name wholly inside a
    #: longer spelled name is a free rider on the longer mention, not a
    #: second competing sense, and telling the two apart needs the actual
    #: matched phrase text, not just the fact that `exact_alias` fired.
    exact_alias_phrases: frozenset[str] = frozenset()
    #: The anchor's own authored name terms the turn shared, when that shared
    #: set is what earned its `lexical_overlap` or `rare_term`. Never
    #: serialised into a packet; `resolve` reads it to tell a qualifier that
    #: narrows two senses from a bare name two senses share.
    name_contact: frozenset[str] = frozenset()
    #: The turn token span `[start, end)` of the longest contiguous run that
    #: spells this anchor's own name words (stopwords may sit inside it, never
    #: at its edges). `None` when no name word was reached. Never serialised.
    name_span: tuple[int, int] | None = None
    entity_type: str = ""
    #: Did the turn capitalise a shared name word away from a sentence start
    #: (`TurnAnalysis.capitalised`)? Never serialised.
    name_capitalised: bool = False
    #: Did a turn whose casing says something write the shared name word
    #: without a capital anywhere (`TurnAnalysis.capitalised_anywhere`)?
    name_lower_case: bool = False

    @property
    def deciding_kinds(self) -> frozenset[str]:
        return self.evidence - TIE_BREAK_KINDS


@dataclass(frozen=True, slots=True)
class ResolvedAnchor:
    """One anchor as the packet reports it."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    status: str
    evidence: tuple[str, ...]
    categories: tuple[str, ...]
    neighbourhood: frozenset[str]
    anchor_neighbourhood: frozenset[str] = frozenset()
    #: Carried over from `CandidateFacts` for R1's own use inside `resolve`
    #: (see that field's docstring). `as_dict` below enumerates its own
    #: fields explicitly and does not list this one, so it never reaches a
    #: served packet.
    exact_alias_phrases: frozenset[str] = frozenset()
    #: Carried over from `CandidateFacts` for `resolve`'s own use, and, like
    #: `exact_alias_phrases`, never serialised.
    name_contact: frozenset[str] = frozenset()
    name_span: tuple[int, int] | None = None
    entity_type: str = ""
    name_capitalised: bool = False
    name_lower_case: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref or self.path or self.anchor_id,
            "path": self.path,
            "title": self.title,
            "kind": self.kind,
            "lifecycle": self.lifecycle,
            "status": self.status,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    """The turn's verdict: which anchors, at what status, and whether to abstain."""

    status: str
    anchors: tuple[ResolvedAnchor, ...]
    ambiguity: tuple[dict[str, Any], ...] = ()
    #: `conversation` when an ambiguous turn was settled by exactly one
    #: competitor the earlier conversation had named; empty otherwise.
    disambiguated_by: str = ""

    @property
    def resolved_anchors(self) -> tuple[ResolvedAnchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.status == "resolved")

    @property
    def partial_anchors(self) -> tuple[ResolvedAnchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.status == "partial")

    @property
    def abstained(self) -> bool:
        return self.status != "resolved"

    @property
    def abstention(self) -> dict[str, str] | None:
        if self.status == "resolved":
            return None
        return {"reason": self.status}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "anchors": [anchor.as_dict() for anchor in self.anchors],
            "ambiguity": [dict(item) for item in self.ambiguity],
            "abstained": self.abstained,
        }


# --------------------------------------------------------------------------- #
# Turn analysis
# --------------------------------------------------------------------------- #


def _depossessive_token(token: str) -> str:
    """One token's possessive-stripped spelling for PHRASE BUILDING (fix/
    activation-competing-senses, correction round 1, C1) -- verbatim when
    `fold_possessive` would land on a STOPWORD, the fold otherwise.

    A contraction of a common pronoun or auxiliary verb ("it's", "let's",
    "that's", "who's"...) folds to a bare function word that would never be
    a turn term or a phrase on its own, and must not become one just
    because it arrived via an apostrophe: "it's finally back online" is not
    a turn naming a product literally called "It". Used for the N-GRAM
    token sequence specifically, where a position must be filled by
    SOMETHING (dropping the token outright would shift every phrase after
    it) — the verbatim spelling is exactly what would have been there
    without this fold in the first place, so substituting it is a true
    no-op at that position, not a different kind of guess.
    """
    folded = fold_possessive(token)
    return token if folded in STOPWORDS else folded


#: Scripts written without spaces between words (scriptio continua), by
#: declared Unicode ranges: a turn token in one of them is a run of words, and
#: an anchor's name can sit inside it. Japanese mixes Han, Hiragana and
#: Katakana within one word, so they are one class. Hangul is not here: Korean
#: separates words with spaces.
_CONTINUA_RANGES: tuple[tuple[str, int, int], ...] = (
    ("cjk", 0x3005, 0x3007),  # 々 〆 〇
    ("cjk", 0x3040, 0x309F),  # Hiragana
    ("cjk", 0x30A0, 0x30FF),  # Katakana
    ("cjk", 0x31F0, 0x31FF),  # Katakana phonetic extensions
    ("cjk", 0x3400, 0x4DBF),  # CJK extension A
    ("cjk", 0x4E00, 0x9FFF),  # CJK unified ideographs
    ("cjk", 0xF900, 0xFAFF),  # CJK compatibility ideographs
    ("cjk", 0xFF66, 0xFF9F),  # halfwidth Katakana
    ("cjk", 0x20000, 0x323AF),  # CJK extensions B-H
    ("thai", 0x0E00, 0x0E7F),
    ("lao", 0x0E80, 0x0EFF),
    ("myanmar", 0x1000, 0x109F),
    ("myanmar", 0xA9E0, 0xA9FF),
    ("myanmar", 0xAA60, 0xAA7F),
    ("khmer", 0x1780, 0x17FF),
    ("khmer", 0x19E0, 0x19FF),
)

#: The lowest code point of any unspaced-script range.
_CONTINUA_FLOOR = min(low for _name, low, _high in _CONTINUA_RANGES)


def _continua_class(text: str) -> str | None:
    """The scriptio-continua class every code point of `text` belongs to, or None."""
    found: str | None = None
    for char in text:
        point = ord(char)
        cls = next((name for name, low, high in _CONTINUA_RANGES if low <= point <= high), None)
        if cls is None or (found is not None and cls != found):
            return None
        found = cls
    return found


def _continua_runs(token: str) -> list[tuple[int, str, str]]:
    """The token's maximal single-class scriptio-continua runs, each with its
    offset in the token and its class. A turn token can glue a Latin word to a
    Japanese phrase (`nameの予算を確認`); its Japanese run still holds names."""
    # Most tokens are Latin or Cyrillic: nothing below the lowest range can
    # belong to a run, so such a token is skipped without a per-character scan.
    if all(ord(char) < _CONTINUA_FLOOR for char in token):
        return []
    runs: list[tuple[int, str, str]] = []
    start, current = 0, None
    for index, char in enumerate(token):
        cls = _continua_class(char)
        if cls != current:
            if current is not None:
                runs.append((start, token[start:index], current))
            start, current = index, cls
    if current is not None:
        runs.append((start, token[start:], current))
    return runs


def _inside_longer_words(word: str, longer: Iterable[str], tokens: Sequence[str]) -> bool:
    """Whether every occurrence of `word` in the turn's tokens lies inside an
    occurrence of a longer word of `longer` (`_contained_names`' rule): a turn
    that also writes the head on its own has not consumed it."""
    others = [other for other in longer if other != word and word in other]
    seen = False
    for token in tokens:
        spans = [
            (start, start + len(other))
            for other in others
            for start in _occurrences(token, other)
        ]
        for start in _occurrences(token, word):
            seen = True
            if not any(low <= start and start + len(word) <= high for low, high in spans):
                return False
    return seen


def _contained_names(
    analysis: TurnAnalysis, rows: Sequence[AnchorFacts], term_counts: Mapping[str, int]
) -> frozenset[str]:
    """Anchors whose name sits inside one of the turn's unspaced runs (design §6.3).

    A name qualifies when it is at least two code points, wholly in the run's
    script class, contained in the run, and rare (`term_anchor_counts` at most
    `RARE_TERM_MAX_ANCHORS`). A name all of whose occurrences lie inside
    another anchor's longer contained name is consumed: the turn spelled the
    longer name, not this one.
    """
    runs = [
        (position, token, offset, run, cls)
        for position, token in enumerate(analysis.tokens)
        for offset, run, cls in _continua_runs(token)
    ]
    if not runs:
        return frozenset()
    found: dict[str, tuple[str, tuple[tuple[int, int, int], ...]]] = {}
    for row in rows:
        best: tuple[str, tuple[tuple[int, int, int], ...]] | None = None
        for name in {normalize(row.title), *row.aliases} - {""}:
            cls = _continua_class(name) if len(name) >= 2 else None
            if cls is None:
                continue
            occurrences = tuple(
                (position, offset + start, offset + start + len(name))
                for position, token, offset, run, run_cls in runs
                if run_cls == cls and token != name
                for start in _occurrences(run, name)
            )
            if occurrences and (best is None or len(name) > len(best[0])):
                best = (name, occurrences)
        if best is not None:
            found[row.anchor_id] = best
    contained: set[str] = set()
    for anchor_id, (name, occurrences) in found.items():
        count = term_counts.get(name)
        if count is None or count > RARE_TERM_MAX_ANCHORS:
            continue
        consumed = all(
            any(
                other_id != anchor_id
                and position == other_position
                and other_start <= start
                and end <= other_end
                and other_end - other_start > end - start
                for other_id, (_other_name, other_occurrences) in found.items()
                for other_position, other_start, other_end in other_occurrences
            )
            for position, start, end in occurrences
        )
        if not consumed:
            contained.add(anchor_id)
    return frozenset(contained)


def embedded_words(
    tokens: Sequence[str], boundaries: frozenset[str] = JAPANESE_PARTICLES
) -> tuple[str, ...]:
    """The words the turn's tokens hold without a space around them.

    Japanese marks its word boundaries with script, not spaces. A Latin word
    glued to a Japanese phrase (`exomemのレイテンシ`) ends where the Japanese
    begins, and inside a Japanese run the particles are written in hiragana,
    so the stretch between two of them (`の白樺を` holds `白樺`) is a word, or
    a compound of words, never the fragment of a longer one. Such a word is
    spelled exactly as a spaced token would be, so a name equal to it is
    `exact_alias`; a name inside a longer stretch (`白樺並木`) is still only
    contained (`_contained_names`).

    A hiragana run is an edge when the WHOLE run is one of `boundaries` (the
    declared particles, plus the vocabulary's filler words from
    `analyze_turn`). With a stretch open, a run that STARTS with a declared
    particle also ends it, matching the longest particle, and the rest of the
    run joins the next stretch: a particle glued to the next kana word
    (`ハヤブサ号はどう`, `をまた`) still marks the name's edge. The unsplit
    stretch is a word too, and so is each cut of it before a particle inside
    the run, so a name that continues through such a run (`サクラもち本舗`,
    `木村はるか`) is an exact match that outranks its head. Any other
    hiragana is part of the stretch around it, so a name written partly in
    hiragana (`ねこやなぎ銀行`, also after `駅前の`) keeps its own edge, and its
    kanji tail (`銀行`) is only contained, as in `ハヤブサ号線`.

    A stretch counts only when it holds at least two code points besides
    hiragana and is not the whole token: one Han character is as often a verb
    stem as a word (`借りた`), the same floor containment has. A run with no hiragana
    (Chinese, Thai) splits only from a glued Latin word.
    """
    words: list[str] = []
    for token in tokens:
        runs = _continua_runs(token)
        if not runs:
            continue
        cursor = 0
        pieces: list[str] = []
        for offset, run, cls in runs:
            if offset > cursor:
                pieces.append(token[cursor:offset])
            cursor = offset + len(run)
            if cls != "cjk":
                pieces.append(run)
                continue
            # `stretch` is split at particles; `full` is the same text with
            # no split, so a name that continues through a hiragana run that
            # merely starts with a particle character still has its spelling.
            stretch = full = ""
            for kana, part in _hiragana_segments(run):
                if kana and part in boundaries:
                    pieces.extend(dict.fromkeys((stretch, full)))
                    stretch = full = ""
                    continue
                if kana and full:
                    # A name can end inside the run, just before a particle
                    # (`木村はるかの`), so each such cut is a candidate too.
                    pieces.extend(
                        full + part[:cut]
                        for cut in range(1, len(part))
                        if _leading_particle(part[cut:])
                    )
                if kana and stretch and (particle := _leading_particle(part)):
                    # A particle glued to the next kana word (はどう, をまた)
                    # still ends the stretch; the rest joins the next one.
                    pieces.append(stretch)
                    stretch = part[len(particle):]
                else:
                    stretch += part
                full += part
            pieces.extend(dict.fromkeys((stretch, full)))
        if cursor < len(token):
            pieces.append(token[cursor:])
        for piece in pieces:
            word = piece.strip("'-")
            if (
                sum(1 for char in word if not is_hiragana(char)) >= 2
                and word != token
                and any(char.isalpha() for char in word)
                and word not in words
            ):
                words.append(word)
    return tuple(words)


#: The declared particles, longest first, for `_leading_particle`.
_PARTICLES_LONGEST_FIRST = tuple(sorted(JAPANESE_PARTICLES, key=len, reverse=True))


def _leading_particle(kana: str) -> str | None:
    """The longest declared particle `kana` starts with, if any."""
    return next((particle for particle in _PARTICLES_LONGEST_FIRST if kana.startswith(particle)), None)


def _hiragana_segments(run: str) -> list[tuple[bool, str]]:
    """`run` as alternating (is hiragana, text) segments."""
    segments: list[tuple[bool, str]] = []
    for char in run:
        kana = is_hiragana(char)
        if segments and segments[-1][0] == kana:
            segments[-1] = (kana, segments[-1][1] + char)
        else:
            segments.append((kana, char))
    return segments


@functools.lru_cache(maxsize=16)
def _word_edges(vocabulary: ReferentialVocabulary) -> frozenset[str]:
    """The declared particles plus the vocabulary's hiragana filler words."""
    return JAPANESE_PARTICLES | frozenset(
        word for word in vocabulary.filler if word and all(is_hiragana(char) for char in word)
    )


@functools.lru_cache(maxsize=16)
def _run_pieces(vocabulary: ReferentialVocabulary) -> tuple[frozenset[str], frozenset[str]]:
    """(cues, cues and filler) spelled wholly in an unspaced script."""
    cues = frozenset(
        phrase for phrase in vocabulary.phrases if " " not in phrase and _continua_class(phrase)
    )
    filler = frozenset(word for word in vocabulary.filler if _continua_class(word))
    return cues, cues | filler


def _spell_out_cues(tokens: Sequence[str], vocabulary: ReferentialVocabulary) -> tuple[str, ...]:
    """`tokens`, with each unspaced token made only of cues and filler spelled
    out word by word (`続けてください` -> `続けて ください`).

    A Japanese turn that points back is one unspaced run, and the words around
    its cue are glued to it. Read word by word, the whole-word cue rule then
    applies unchanged: the turn is referential only if it holds a cue and
    nothing besides filler. A token that does not split wholly into those
    words is left as it is, so any word of its own keeps it a residue.
    """
    cues, pieces = _run_pieces(vocabulary)
    if not cues:
        return tuple(tokens)
    out: list[str] = []
    for token in tokens:
        split = _split_into(token, pieces) if _continua_class(token) else None
        if split is not None and any(part in cues for part in split):
            out.extend(split)
        else:
            out.append(token)
    return tuple(out)


def _split_into(text: str, pieces: frozenset[str]) -> tuple[str, ...] | None:
    """One split of `text` into members of `pieces`, fewest parts first."""
    if text in pieces:
        return (text,)
    longest = max(map(len, pieces))
    best: list[tuple[str, ...] | None] = [None] * (len(text) + 1)
    best[0] = ()
    for end in range(1, len(text) + 1):
        for start in range(max(0, end - longest), end):
            head = best[start]
            if head is None or text[start:end] not in pieces:
                continue
            if best[end] is None or len(head) + 1 < len(best[end]):
                best[end] = (*head, text[start:end])
    return best[len(text)]


def _occurrences(text: str, name: str) -> list[int]:
    starts: list[int] = []
    start = text.find(name)
    while start >= 0:
        starts.append(start)
        start = text.find(name, start + 1)
    return starts


def _clears_rare_term_length(term: str, *, acronyms: frozenset[str] = frozenset()) -> bool:
    """Is `term` long enough to be a lead?

    `RARE_TERM_MIN_CHARS` code points, for an all-ASCII-letter term only.
    Any term carrying a character outside `a-z` is exempt: the floor's whole
    argument is about English word lengths, and a script that writes a name
    in two characters is not the case it was reasoned about. So is a term in
    `acronyms` — the caller's intersection of the turn's two-capital words
    with the anchor title's (`RARE_TERM_MIN_CHARS`).
    """
    return (
        len(term) >= RARE_TERM_MIN_CHARS
        or not term.isascii()
        or not term.isalpha()
        or term in acronyms
    )


def _acronyms_of(text: str, filler: frozenset[str] = frozenset()) -> frozenset[str]:
    """The casefolded two-capital words `text` spells, or nothing when the
    whole text is in capitals (caps lock says nothing about any one word).

    Used on a turn and on an anchor's title alike. Function words and the
    referential filler are left out: "SO" or "OK" in capitals name nothing,
    and "OK continue" must not reach an anchor titled "OK Go" by its "OK".
    """
    raw = unicodedata.normalize("NFKC", str(text or ""))
    if not any(character.islower() for character in raw):
        return frozenset()
    return frozenset(
        folded
        for word in _ACRONYM_RE.findall(raw)
        if (folded := word.casefold()) not in _STOPWORDS and folded not in filler
    )


def _fold_lexical_term(term: str) -> str | None:
    """One term's fold for the LEXICAL COMPARISON (fix/activation-competing-
    senses, correction round 1, C1) -- `fold_plural(fold_possessive(term))`,
    or `None` when the possessive fold alone already lands on a STOPWORD.

    Unlike `_depossessive_token`, a term set has no positions to preserve, so
    a term whose fold is unsound is simply DROPPED rather than kept verbatim
    -- the verbatim spelling ("it's") would never match anything anyway, so
    dropping it costs nothing.

    Used identically on BOTH sides of the lexical comparison in
    `candidates_for` (the turn's own terms AND the anchor's `row.terms`/
    title+alias terms): the same function on both call sites is what makes
    the anchor side symmetric with the turn side by construction — a title
    "It's Complicated" cannot manufacture the name term "it" any more than a
    turn saying "it's" can, because both route through this one function.
    """
    folded = fold_possessive(term)
    if folded in STOPWORDS:
        return None
    return fold_plural(folded)


def _referential_residue(
    token_text: str, vocabulary: ReferentialVocabulary
) -> tuple[str, ...]:
    """The words a cue-speaking turn says besides its cues, function words and
    the vocabulary's filler — empty for a turn that only points back.

    `token_text` is the turn's tokens joined by single spaces and padded with
    one on each side, the form `analyze_turn` matches cues against.
    """
    text = token_text
    for phrase in vocabulary.phrases:
        while f" {phrase} " in text:
            text = text.replace(f" {phrase} ", " ")
    return tuple(
        token
        for token in text.split()
        if token not in _STOPWORDS and token not in vocabulary.filler
    )


def analyze_turn(turn: str, *, vocabulary: ReferentialVocabulary | None = None) -> TurnAnalysis:
    """Normalise a raw turn once: NFKC + casefold, tokens, n-grams.

    `vocabulary` is the effective referential vocabulary from the vault's
    activation conventions (`activation_conventions.load_conventions(root)
    .conventions.referential`); a caller with no vault gets the shipped seed.
    """
    if vocabulary is None:
        vocabulary = shipped_vocabulary()
    # Calls the shared `normalize()` rather than restating its formula: a
    # hand-rolled copy here once skipped `normalize()`'s typographic-
    # apostrophe fold, so a turn spelled with a curly quote matched none of
    # a role cue's plain-apostrophe substrings (e.g. "i'm planning") even
    # though every OTHER comparison in this module already folded it.
    text = normalize(turn)
    # Order and repetitions are kept: the n-gram window below must be able to
    # start a phrase at a word the turn has already used, or a turn naming two
    # anchors that share a word can only ever reach the first of them. Callers
    # that want a term set take one from these tokens themselves.
    tokens = tokens_of(text)
    # R4 (fix/activation-competing-senses): n-grams of the DE-POSSESSIVED
    # token sequence, IN ADDITION to the verbatim ones -- "should I replace
    # the gamma's sensor" must be able to reach a spelled-out "Gamma
    # Fleet" through the phrase "gamma fleet" it would never form verbatim,
    # while "Dana's Plan", authored with a possessive, must still match a
    # turn spelling it out the same way (the verbatim pass, unchanged).
    folded_tokens = tuple(_depossessive_token(token) for token in tokens)
    ngrams: list[str] = []
    seen: set[str] = set()
    for token_sequence in (tokens, folded_tokens):
        for size in range(2, MAX_NGRAM + 1):
            for start in range(0, max(0, len(token_sequence) - size + 1)):
                phrase = " ".join(token_sequence[start : start + size])
                if phrase not in seen:
                    seen.add(phrase)
                    ngrams.append(phrase)
    token_text = f" {' '.join(_spell_out_cues(tokens, vocabulary))} "
    # A declared cue, and nothing else said (close-memory-loop D2, as
    # narrowed twice): the turn has to say it points back, and must not also
    # say what it is about.
    referential_cue = any(f" {phrase} " in token_text for phrase in vocabulary.phrases)
    referential = referential_cue and not _referential_residue(token_text, vocabulary)
    pointing = working_set_anaphora.points_back(
        tokens, referential_cue=referential_cue, follow_up_markers=FOLLOW_UP_MARKERS
    )
    content = working_set_anaphora.content_words(tokens, vocabulary=_vocabulary_words(vocabulary))
    return TurnAnalysis(
        text=text,
        tokens=tokens,
        ngrams=tuple(ngrams),
        referential=referential,
        referential_cue=referential_cue,
        acronyms=_acronyms_of(turn, vocabulary.filler),
        follow_up=is_follow_up(
            tokens, referential_cue=referential_cue, filler=vocabulary.filler
        ),
        words=embedded_words(tokens, _word_edges(vocabulary)),
        capitalised=_capitalised_terms(turn),
        run_breaks=_run_breaks(text, tokens),
        cased_turn=_casing_signal(turn),
        capitalised_anywhere=_capitalised_terms(turn, anywhere=True),
        points_back=pointing,
        content_words=content,
        anaphoric=pointing and not content,
    )


#: Clause punctuation, for `_run_breaks`: sentence ends, comma, colon,
#: semicolon, parentheses and square brackets, and dashes (an en or em dash, or a hyphen standing alone between
#: spaces; a hyphen inside a word is part of the word), in the cased scripts'
#: and the CJK forms.
_CLAUSE_BREAK = re.compile(
    r"[.,:;!?()\[\]\n\r\u2026\u2013\u2014\u3001\u3002\uff01\uff0c\uff0e\uff1a\uff1b\uff1f]+"
    r"|(?:^|\s)-+(?=\s|$)"
)
#: The coordinators the tokeniser's stopwords already carry: "X and Y" is two
#: things, so neither ends up inside one run of name words.
_RUN_COORDINATORS: frozenset[str] = frozenset({"and", "or"})


def _run_breaks(text: str, tokens: Sequence[str]) -> frozenset[int]:
    """Token indices with clause punctuation just before them in `text`.

    Tokenises each punctuation-delimited segment with `tokens_of`; when that
    does not reproduce `tokens` exactly the positions cannot be trusted and
    none are reported (a run then breaks only at coordinators).
    """
    breaks: set[int] = set()
    seen: list[str] = []
    for segment in _CLAUSE_BREAK.split(text):
        if seen:
            breaks.add(len(seen))
        seen.extend(tokens_of(segment))
    if tuple(seen) != tuple(tokens):
        return frozenset()
    return frozenset(index for index in breaks if 0 < index < len(tokens))


#: Where a sentence ends, for `_capitalised_terms`: the full stops, question
#: and exclamation marks and semicolons of the cased scripts, and a line break.
_SENTENCE_END = re.compile(r"[.!?;\n\r\u2026]+")
_RAW_WORD = re.compile(r"[^\W_][\w'\u2019-]*")


def _casing_signal(turn: str) -> bool:
    """Does the raw turn's casing carry information?

    Only a turn that writes some cased word with a capital AND some cased word
    without one does. An all-lower-case turn, an all-caps turn and a headline
    whose every word is capitalised mark no word as a name.
    """
    raw = unicodedata.normalize("NFKC", str(turn or ""))
    upper = lower = False
    for word in _RAW_WORD.findall(raw):
        if not _is_cased(word[:1]):
            continue
        if word[:1].isupper():
            upper = True
        else:
            lower = True
    return upper and lower


def _capitalised_terms(turn: str, *, anywhere: bool = False) -> frozenset[str]:
    """Words the raw turn writes with a capital other than as a sentence's
    first word, folded as lexical terms are (`TurnAnalysis.capitalised`).
    `anywhere` counts a sentence's first word too."""
    raw = unicodedata.normalize("NFKC", str(turn or ""))
    if not _casing_signal(raw):
        return frozenset()
    out: set[str] = set()
    for sentence in _SENTENCE_END.split(raw):
        for position, word in enumerate(_RAW_WORD.findall(sentence)):
            if (position == 0 and not anywhere) or not word[:1].isupper():
                continue
            for token in tokens_of(normalize(word)):
                folded = _fold_lexical_term(token)
                if folded is not None:
                    out.add(folded)
    return frozenset(out)


def _is_cased(term: str) -> bool:
    """Does `term`'s script distinguish capitals at all?"""
    return any(character.upper() != character.lower() for character in term)


# --------------------------------------------------------------------------- #
# Role-cue evidence (make-activation-conventions-vault-owned, decision 1)
# --------------------------------------------------------------------------- #


def _is_evidence_cue(cue: str, turn_tokens: Sequence[str]) -> bool:
    """The three bounds, in order: long enough, tokenises, whole terms in
    order. `tokens_of` normalises its own input (NFKC + casefold, the same
    typographic apostrophe/hyphen fold `turn_tokens` was produced with), so
    a cue authored either way still lines up with the turn."""
    if len(cue) < MIN_EVIDENCE_CUE_CHARS:
        return False
    cue_tokens = tokens_of(cue)
    span = len(cue_tokens)
    if span == 0 or span > len(turn_tokens):
        return False
    return any(
        tuple(turn_tokens[start : start + span]) == cue_tokens
        for start in range(len(turn_tokens) - span + 1)
    )


def eligible_categories(analysis: TurnAnalysis, roles: Iterable[Any]) -> frozenset[str]:
    """Categories `category_match` may draw on for this turn: the union of
    `evidence_categories` over every role with an EVIDENCE cue in the turn
    (design `make-activation-conventions-vault-owned`, decision 1).

    `roles` is whatever iterable of role-like objects the caller's registry
    exposes (`context_roles.RoleRegistry.roles.values()` in production) --
    each read only for `.evidence_cues` and `.evidence_categories`, so this
    function stays free of any dependency on `context_roles`'s own types.
    A cue failing a bound (a bare `?`, for instance) can still SELECT its
    role -- `context_roles.select_roles` matches every cue as a plain
    substring -- but never contributes a category here.
    """
    categories: set[str] = set()
    turn_tokens = analysis.tokens
    for role in roles:
        role_categories = getattr(role, "evidence_categories", None)
        role_cues = getattr(role, "evidence_cues", None)
        if not role_categories or not role_cues:
            continue
        if any(_is_evidence_cue(cue, turn_tokens) for cue in role_cues):
            categories.update(role_categories)
    return frozenset(categories)


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


class RowLexicon(NamedTuple):
    """One row's words as `candidates_for` compares them, derived once."""

    names: frozenset[str]
    name_terms: frozenset[str]
    terms: frozenset[str]


def row_lexicon(row: AnchorFacts) -> RowLexicon:
    """`row`'s normalised names, the folded terms of its title and aliases,
    and the folded terms of its whole vocabulary."""
    return RowLexicon(
        names=frozenset({normalize(row.title), *row.aliases} - {""}),
        name_terms=frozenset(
            folded
            for term in tokens_of(" ".join((row.title, *row.aliases)))
            if (folded := _fold_lexical_term(term)) is not None
        ),
        terms=frozenset(
            folded for term in row.terms if (folded := _fold_lexical_term(term)) is not None
        ),
    )


def candidates_for(
    analysis: TurnAnalysis,
    rows: Sequence[AnchorFacts],
    *,
    vectors: Mapping[str, Any] | None = None,
    query_vector: Any | None = None,
    bands: Mapping[str, bool] | None = None,
    routing_targets: Sequence[Any] = (),
    retrieval_paths: frozenset[str] = frozenset(),
    used_paths: frozenset[str] = frozenset(),
    hot_paths: frozenset[str] = frozenset(),
    term_anchor_counts: Mapping[str, int] | None = None,
    config: RankingConfig | None = None,
    stopwords: frozenset[str] = _STOPWORDS,
    rare_term_max_anchors: int = RARE_TERM_MAX_ANCHORS,
    eligible_categories: frozenset[str] = frozenset(),
    row_lexicons: Mapping[str, RowLexicon] | None = None,
) -> tuple[CandidateFacts, ...]:
    """Assemble categorical evidence for every anchor this turn can reach.

    `row_lexicons` (anchor id -> `row_lexicon(row)`) lets a caller matching
    several turns against the same rows derive each row's names once
    (`candidates_for_each`); without it each row is derived here.

    `term_anchor_counts` is the index's title/alias term -> anchor-count table
    (`WorkingSetIndex.term_anchor_counts()`), the structure `rare_term`'s
    rarity check is measured against. Absent (`None`) simply means no anchor
    can earn `rare_term` this call — never a fabricated rarity.

    `bands` is `vector_bands`' decision, computed where the turn is encoded;
    without it the band is computed here from `vectors` and `query_vector`.

    `hot_paths` is the top of the caller's recency profile
    (`working_set.hot_profile`), passed in like `used_paths` because it is a
    fact about the vault, not about the turn. It earns `recency`, and on a
    REFERENTIAL turn only it also admits an anchor the turn's own words never
    reached — the one widening design §8 allows, so that a turn whose whole
    content is a reference has something to refer to. Whether such a
    candidate then resolves is `resolve()`'s call, not this function's: the
    condition is about the whole candidate set.

    `stopwords` and `rare_term_max_anchors` default to the shipped values;
    the real build passes the vault's EFFECTIVE activation-conventions
    registry (`make-activation-conventions-vault-owned`), read once per
    build in `working_set.compile_packet` and passed to both this function
    and the index's own derived-short-name admission, so a turn's own words
    and an anchor's derived alias are measured against the same list.

    `eligible_categories` defaults to none: this function stays registry-free
    (`analysis` carries no cues of its own), so `category_match` is only ever
    considered when the caller passes the categories the vault's roles
    registry made eligible for THIS turn -- `working_set.compile_packet`
    computes it once per build via `eligible_categories()` above and passes
    it through, the same pattern `stopwords`/`rare_term_max_anchors` already
    follow.
    """
    config = config or DEFAULT_RANKING
    term_counts = term_anchor_counts or {}
    turn_terms = frozenset((*analysis.tokens, *analysis.words)) - stopwords
    # R4 (fix/activation-competing-senses): possessive fold, applied on the
    # TURN side of the lexical comparison -- "gamma's" must contribute the
    # term "gamma" the same way a plural turn token already folds to its
    # singular. `_fold_lexical_term` (C1, correction round 1) is
    # `fold_plural(fold_possessive(term))` with one guard: a term whose
    # possessive fold alone lands on a STOPWORD ("it's" -> "it") is dropped
    # rather than folded, so an ordinary contraction of a common pronoun or
    # auxiliary verb can never manufacture a turn term that was never typed.
    turn_terms_folded = frozenset(
        folded for term in turn_terms if (folded := _fold_lexical_term(term)) is not None
    )
    # Terms only an embedded word supplies (`analysis.words`: a Latin word glued
    # to Japanese). They hold no position in `analysis.tokens`, so a name span
    # over the tokens cannot see them, and a contact that rests on one has no
    # span to narrow by (`name_span` below).
    embedded_terms = turn_terms_folded - frozenset(
        folded
        for term in frozenset(analysis.tokens) - stopwords
        if (folded := _fold_lexical_term(term)) is not None
    )
    # A single turn TOKEN is a phrase too (`phrases` also feeds unigram
    # `exact_alias` matches), so its de-possessived spelling joins the
    # phrase set alongside the verbatim one -- "dana's" must reach a plain
    # single-word `exact_alias` on "Dana" exactly as "dana" already would.
    # `analysis.ngrams` already carries the de-possessived MULTI-token
    # phrases (`analyze_turn`); this adds the one-token case `analyze_turn`
    # never builds n-grams for. Same C1 guard: a fold that lands on a
    # STOPWORD is dropped, never added as a phrase (the verbatim spelling is
    # already covered by `frozenset(analysis.tokens)` above, so nothing is
    # lost by not also adding its unsound fold).
    phrases = (
        frozenset(analysis.ngrams)
        | frozenset(analysis.tokens)
        | frozenset(analysis.words)
        | frozenset(
            folded
            for token in analysis.tokens
            if (folded := fold_possessive(token)) not in stopwords
        )
    )
    cue_categories = eligible_categories
    claims_winner = _claims_winner(analysis, routing_targets)
    if bands is None:
        bands = _vector_bands(rows, vectors, query_vector, config) if query_vector is not None else {}
    # A floor of 2, whatever `RankingConfig` says (design.md decision 2a):
    # the two-shared-terms minimum is part of the soundness argument, so it
    # lives in code, not in an operator-tunable file. The shipped default is
    # already 2, so this never changes shipped behaviour.
    min_terms = max(2, int(config.working_set_lexical_min_terms))

    # R2 (fix/activation-competing-senses), pass 1 of 2: each row's own
    # matched `exact_alias` phrases, and the turn TOKEN POSITIONS any
    # MULTI-token (two or more tokens) one covers. A single-token phrase
    # covers nothing: "consumption" is a property of a SPELLED-OUT name, not
    # of one shared word standing alone
    # (`test_r2_single_token_aliases_consume_nothing`). Linear in
    # rows x phrases, no index read: positions come from the turn's own
    # tokens, already in hand.
    # Both passes below read each row's lexicon: derive it once per call.
    lexicons: dict[str, RowLexicon] = dict(row_lexicons or {})

    def lexicon_of(row: AnchorFacts) -> RowLexicon:
        known = lexicons.get(row.anchor_id)
        if known is None:
            known = lexicons[row.anchor_id] = row_lexicon(row)
        return known

    row_exact_phrases: dict[str, frozenset[str]] = {}
    own_covered: dict[str, frozenset[int]] = {}
    covered_positions: set[int] = set()
    # An embedded word that only spells the head of a longer embedded word some
    # indexed name equals (`サクラ` inside `サクラもち本舗`) is consumed: the
    # turn wrote the longer name, the same way containment consumes a name.
    embedded_only = frozenset(analysis.words) - frozenset(analysis.tokens) - frozenset(analysis.ngrams)
    matched_words = {
        word
        for row in rows
        for word in ({normalize(row.title), *row.aliases} & frozenset(analysis.words))
    }
    consumed_words = frozenset(
        word
        for word in embedded_only
        if _inside_longer_words(word, matched_words, analysis.tokens)
    )
    for row in rows:
        names = lexicon_of(row).names
        matched = (names & phrases) - consumed_words
        row_exact_phrases[row.anchor_id] = frozenset(matched)
        positions: set[int] = set()
        for phrase in matched:
            phrase_tokens = phrase.split(" ")
            if len(phrase_tokens) < 2:
                continue
            for start, end in _phrase_spans(analysis.tokens, phrase_tokens):
                positions.update(range(start, end))
        own_covered[row.anchor_id] = frozenset(positions)
        covered_positions.update(positions)

    # Position of every non-stopword turn token, by its FOLDED form — used
    # only by the consumption check below, the SAME comparison key
    # `only_shared_name_term` already uses (`_fold_lexical_term`, matching
    # `turn_terms_folded`'s own construction above).
    term_positions: dict[str, list[int]] = {}
    for index, token in enumerate(analysis.tokens):
        if token in stopwords:
            continue
        folded_term = _fold_lexical_term(token)
        if folded_term is None:
            continue
        term_positions.setdefault(folded_term, []).append(index)

    contained = _contained_names(analysis, rows, term_counts)
    # The turn's own folds, once: `_name_span` reads them for every candidate.
    token_folds = _token_folds(analysis.tokens, stopwords)

    # Pass 2 of 2: the ordinary per-row evidence assembly, reusing pass 1's
    # own matched phrases rather than recomputing them.
    out: list[CandidateFacts] = []
    for row in rows:
        evidence: set[str] = set()
        matched_phrases = row_exact_phrases[row.anchor_id]
        if matched_phrases:
            evidence.add("exact_alias")
        # `lexical_overlap` and `rare_term` are mutually exclusive on one
        # anchor (review round 4, BLOCKER): both were being read off the SAME
        # intersection of the turn's words with the anchor's, so one fact —
        # the turn shares words with this anchor — was counted twice and
        # `_status_for` resolved the pair. `lexical_overlap` needs at least
        # `min_terms` shared words over the BROAD vocabulary (title, aliases,
        # sections, tags) AND genuine NAME CONTACT among the anchor's OWN
        # AUTHORED title/alias terms — a tag or section word may COMPLETE an
        # overlap, never CONSTITUTE one alone, or two anchors sharing nothing
        # but a tag and a section heading (e.g. two `hub`-tagged pages each
        # with a `## Notes` section) would "overlap" on words neither one
        # authored. `rare_term` is granted only when `lexical_overlap` was
        # NOT: it is the single-authored-rare-word case, a genuinely weaker,
        # different fact, never a second vote for the same one.
        #
        # Name contact (`close-memory-loop`, root cause 2, correction round
        # 1) is TWO OR MORE shared AUTHORED name terms — never one, however
        # rare `term_anchor_counts` shows that one term to be. The first
        # attempt at this rule also granted `lexical_overlap` for a single
        # shared name term that was independently rare, reasoning that
        # "named by few anchors" meant "a rare word" — but rarity among
        # ANCHOR NAMES is not rarity of the WORD: a common English word can
        # easily name three or fewer anchors in a small catalogue while
        # remaining an everyday word no turn "names" an anchor by using. The
        # orchestrator's offline measurement against production-scale state
        # confirmed exactly that shape still resolved an unnamed hub. A
        # single shared name term — rare or not — can now only ever earn
        # `rare_term` below, and `_status_for`'s own third clause already
        # requires a second, independently reached CONTACT kind (never a
        # mere qualifier) before `rare_term` resolves anything alone.
        # R4 (fix/activation-competing-senses): the SAME possessive fold on
        # the ANCHOR side too, for symmetry with the turn side above -- a
        # title authored "Dana's Plan" shares the name term "dana" with a
        # turn saying plain "dana", not just the other way around. C1
        # (correction round 1): the SAME `_fold_lexical_term` the turn side
        # uses, so a title "It's Complicated" cannot manufacture the name
        # term "it" any more than a turn saying "it's" can -- one function,
        # both call sites, symmetric by construction.
        lexicon = lexicon_of(row)
        row_terms_folded = lexicon.terms
        name_terms_folded = lexicon.name_terms
        shared_broad = turn_terms_folded & row_terms_folded
        shared_name = turn_terms_folded & name_terms_folded
        name_contact: frozenset[str] = frozenset()
        if len(shared_broad) >= min_terms and len(shared_name) >= 2:
            evidence.add("lexical_overlap")
            name_contact = shared_name
        elif len(shared_name) == 1:
            (only_shared_name_term,) = shared_name
            count = term_counts.get(only_shared_name_term)
            if (
                _clears_rare_term_length(
                    only_shared_name_term,
                    acronyms=analysis.acronyms & _acronyms_of(row.title)
                    if analysis.acronyms
                    else frozenset(),
                )
                and count is not None
                and count <= rare_term_max_anchors
            ):
                # R2: a turn term all of whose occurrences lie inside the
                # token span of a DIFFERENT anchor's own spelled-out
                # multi-token name is consumed and cannot be the single
                # shared name term that earns `rare_term` here — the turn
                # never used the word as a lead to THIS anchor, only as
                # part of spelling out the other one's. Still counts for the
                # anchor whose own alias did the covering (`position not in
                # own_covered[row.anchor_id]` below is false for that row,
                # so `consumed` is false and `rare_term` is granted).
                positions = term_positions.get(only_shared_name_term, ())
                consumed = bool(positions) and all(
                    position in covered_positions
                    and position not in own_covered[row.anchor_id]
                    for position in positions
                )
                if not consumed:
                    evidence.add("rare_term")
                    name_contact = shared_name
        # An unspaced script writes a name inside a run of words, never as a
        # token of its own: containment is its `rare_term` (design §6.3),
        # never `exact_alias`, and like any `rare_term` it needs a second,
        # independent contact to resolve.
        if (
            row.anchor_id in contained
            and not matched_phrases
            and not evidence & {"lexical_overlap", "rare_term"}
        ):
            evidence.add("rare_term")
        if bands.get(row.anchor_id):
            evidence.add("vector_band")
        if claims_winner is not None and claims_winner == row.path:
            evidence.add("claims_match")
        # The anchor's OWN page, never a page in its neighbourhood: recall
        # returns hits for every turn, and a hub or a person links dozens of
        # pages, so a neighbour hit is not the turn reaching the anchor —
        # it is corroborated instead, and only from a WORDED partner (see
        # `add_graph_corroboration`).
        if row.path and row.path in retrieval_paths:
            evidence.add("retrieval")
        # Qualifiers, applied only to an anchor the turn already reached. An
        # anchor with no contact kind is not a candidate at all — unless the
        # turn is referential and this anchor is at the top of the hot
        # profile, which is the one case where the absence of worded contact
        # is the point rather than a disqualification.
        hot = bool(row.path) and row.path in hot_paths
        if not evidence & CONTACT_KINDS and not (hot and analysis.referential):
            continue
        if hot:
            evidence.add("recency")
        if cue_categories and cue_categories & frozenset(row.categories):
            evidence.add("category_match")
        if row.path and row.path in used_paths:
            evidence.add("usage_prior")
        out.append(
            CandidateFacts(
                anchor_id=row.anchor_id,
                path=row.path,
                ref=row.ref,
                title=row.title,
                kind=row.kind,
                lifecycle=row.lifecycle,
                categories=row.categories,
                neighbourhood=row.neighbourhood,
                anchor_neighbourhood=row.anchor_neighbourhood,
                evidence=frozenset(evidence),
                exact_alias_phrases=matched_phrases,
                name_contact=name_contact,
                name_span=_name_span(
                    analysis.tokens,
                    stopwords,
                    name_terms_folded,
                    analysis.run_breaks,
                    token_folds,
                )
                if name_contact and not name_contact & embedded_terms
                else None,
                entity_type=row.entity_type,
                name_capitalised=bool(name_contact & analysis.capitalised),
                name_lower_case=bool(
                    name_contact
                    and analysis.cased_turn
                    and all(_is_cased(term) for term in name_contact)
                    and not name_contact & analysis.capitalised_anywhere
                ),
            )
        )
    out.sort(key=_candidate_order)
    if analysis.referential:
        # The hot candidates survive the cut on a referential turn. Only they
        # can resolve it, and they carry no contact kind, so the ordinary
        # order ranks them behind every recall hit the turn's filler words
        # happened to reach — six of those were enough to drop the referent
        # before `resolve()` ever saw it. Bounded by `hot_paths`, which the
        # caller cuts at `working_set.HOT_PROFILE_K`; every other turn is cut
        # exactly as before.
        hot = [item for item in out if "recency" in item.evidence][:MAX_CANDIDATES]
        rest = [item for item in out if "recency" not in item.evidence]
        return tuple(sorted([*hot, *rest[: MAX_CANDIDATES - len(hot)]], key=_candidate_order))
    return tuple(out[:MAX_CANDIDATES])



#: `candidates_for` arguments that can give an anchor contact without the
#: turn's own words naming it. `candidates_for_each` may only narrow the rows
#: when none of them is in play.
_WORDLESS_CONTACT_ARGUMENTS = (
    "vectors",
    "query_vector",
    "bands",
    "routing_targets",
    "retrieval_paths",
    "hot_paths",
)


def _row_may_be_named(
    lexicon: RowLexicon, terms: frozenset[str], phrases: frozenset[str], runs: bool
) -> bool:
    """Could any analysis whose folded terms and phrases are these reach the
    row by its words? A superset test: every row `candidates_for` could
    return, or read another row's consumption from, passes it."""
    if lexicon.names & phrases or lexicon.name_terms & terms:
        return True
    return runs and any(len(name) >= 2 and _continua_class(name) for name in lexicon.names)


def candidates_for_each(
    analyses: Sequence[TurnAnalysis], rows: Sequence[AnchorFacts], **keywords: Any
) -> tuple[tuple[CandidateFacts, ...], ...]:
    """`candidates_for` for several analyses in ONE scan over `rows`.

    The earlier entries of a conversation are matched by their words only (no
    band, recall, routing or recency). Only a row whose own name shares a
    word or a phrase with SOME entry can be reached, or consume a term for
    another row, so the full catalogue is scanned once for those, and each
    entry is then matched over that short list: the result is identical to
    one `candidates_for` per entry at a fraction of the cost. With any
    wordless contact source in `keywords` it falls back to one full scan each.
    """
    if not analyses:
        return ()
    if any(keywords.get(name) for name in _WORDLESS_CONTACT_ARGUMENTS):
        return tuple(candidates_for(analysis, rows, **keywords) for analysis in analyses)
    stopwords = keywords.get("stopwords", _STOPWORDS)
    terms: set[str] = set()
    phrases: set[str] = set()
    runs = False
    for analysis in analyses:
        for term in frozenset((*analysis.tokens, *analysis.words)) - stopwords:
            folded = _fold_lexical_term(term)
            if folded is not None:
                terms.add(folded)
        phrases.update(analysis.ngrams, analysis.tokens, analysis.words)
        phrases.update(
            folded for token in analysis.tokens if (folded := fold_possessive(token)) not in stopwords
        )
        runs = runs or any(_continua_runs(token) for token in analysis.tokens)
    frozen_terms, frozen_phrases = frozenset(terms), frozenset(phrases)
    lexicons: dict[str, RowLexicon] = {}
    named: list[AnchorFacts] = []
    for row in rows:
        lexicon = row_lexicon(row)
        if _row_may_be_named(lexicon, frozen_terms, frozen_phrases, runs):
            lexicons[row.anchor_id] = lexicon
            named.append(row)
    return tuple(
        candidates_for(analysis, named, row_lexicons=lexicons, **keywords) for analysis in analyses
    )

def _token_folds(tokens: Sequence[str], stopwords: frozenset[str]) -> tuple[str | None, ...]:
    """Each token's lexical fold, `None` for a stopword: what `_name_span`
    compares against a name's terms, computed once per turn."""
    return tuple(None if token in stopwords else _fold_lexical_term(token) for token in tokens)


def _name_span(
    tokens: Sequence[str],
    stopwords: frozenset[str],
    name_terms: frozenset[str],
    breaks: frozenset[int] = frozenset(),
    folds: Sequence[str | None] | None = None,
) -> tuple[int, int] | None:
    """The longest contiguous run of `tokens` spelling `name_terms` words.

    Stopwords may sit inside a run ("bank of the north") but never start or
    end one, and any other word breaks it. So does clause punctuation
    (`breaks`: token indices with a boundary just before them) and a
    coordinator ("and", "or"): "the solar array, monitoring" and "the solar
    array and monitoring" are two things, not one name. Ties go to the
    earliest run, so the span is deterministic. `None` when no token is a
    name word. `folds` is each token's `_fold_lexical_term` (`None` for a
    stopword), computed once per turn by a caller that spans many names.
    """
    if folds is None:
        folds = _token_folds(tokens, stopwords)
    best: tuple[int, int] | None = None
    best_words = 0
    start: int | None = None
    last_name = -1
    words = 0
    for index, token in enumerate(tokens):
        if index in breaks or token in _RUN_COORDINATORS:
            start = None
            if token in _RUN_COORDINATORS:
                continue
        folded = folds[index]
        if folded is not None and folded in name_terms:
            if start is None:
                start, words = index, 0
            last_name = index
            words += 1
            if words > best_words:
                best, best_words = (start, last_name + 1), words
        elif token in stopwords and start is not None:
            continue
        else:
            start = None
    return best


def _derived_key(title: str) -> str:
    derived = derived_short_name(title)
    return normalize(derived) if derived else ""


def _authored_names(row: AnchorFacts) -> tuple[str, ...]:
    """The title and the aliases the index did not derive from the title."""
    derived = _derived_key(row.title)
    return (row.title, *(alias for alias in row.aliases if alias != derived))


def _name_terms(names: Iterable[str]) -> frozenset[str]:
    """Name terms as `candidates_for` compares them with the turn's."""
    return frozenset(
        folded
        for term in tokens_of(" ".join(names))
        if (folded := _fold_lexical_term(term)) is not None
    )


def _counted_terms(names: Iterable[str]) -> frozenset[str]:
    """Name terms as the index counts their owners (`term_anchor_counts`)."""
    return frozenset(fold_plural(term) for term in tokens_of(" ".join(names)))


def audience_view(
    analysis: TurnAnalysis,
    rows: Sequence[AnchorFacts],
    term_anchor_counts: Mapping[str, int] | None,
    visible: Callable[[str], bool],
) -> tuple[tuple[AnchorFacts, ...], dict[str, int], frozenset[str]]:
    """This turn's catalogue as a reader other than the owner sees it.

    Names, aliases and name-term counts are computed over every anchor when
    the index is built, so an anchor the reader may not see can make a visible
    anchor's name ambiguous, a turn word common, or retire a derived short
    name. This decides, lazily, only the anchors the turn's words can reach
    (by name, alias, derived short name or name term), stopping a term's count
    once it is common among visible anchors. It returns the rows without the
    anchors decided withheld, the name-term counts over visible owners, and the
    ids of the anchors it decided. A derived short name only a withheld anchor
    retired is restored. An anchor with no page names nothing to withhold.

    Nothing here reads the vault except the decisions themselves, so the
    request stays within the activation path's filesystem ceilings.
    """
    counts = dict(term_anchor_counts or {})
    phrases = (
        frozenset(analysis.ngrams)
        | frozenset(analysis.tokens)
        | frozenset(analysis.words)
        | frozenset(
            folded
            for token in analysis.tokens
            if (folded := fold_possessive(token)) not in STOPWORDS
        )
    )
    turn_terms = frozenset(
        folded
        for term in frozenset((*analysis.tokens, *analysis.words)) - _STOPWORDS
        if (folded := _fold_lexical_term(term)) is not None
    )
    decided: dict[str, bool] = {}

    def seen(row: AnchorFacts) -> bool:
        verdict = decided.get(row.anchor_id)
        if verdict is None:
            verdict = not row.path or bool(visible(row.path))
            decided[row.anchor_id] = verdict
        return verdict

    owners: dict[str, list[AnchorFacts]] = {}
    derived_holders: dict[str, list[AnchorFacts]] = {}
    for row in rows:
        names = {normalize(name) for name in (row.title, *row.aliases)} - {""}
        if names & phrases or len(_name_terms((row.title, *row.aliases)) & turn_terms) >= 2:
            seen(row)
        for term in _counted_terms(_authored_names(row)) & turn_terms:
            owners.setdefault(term, []).append(row)
        derived = _derived_key(row.title)
        if derived:
            derived_holders.setdefault(derived, []).append(row)

    for term, term_owners in sorted(owners.items()):
        withheld = 0
        visible_count = 0
        for row in sorted(term_owners, key=lambda item: item.anchor_id):
            if seen(row):
                visible_count += 1
                if visible_count > RARE_TERM_MAX_ANCHORS:
                    break
            else:
                withheld += 1
        else:
            if term in counts:
                counts[term] = max(0, counts[term] - withheld)

    restored: dict[str, tuple[str, ...]] = {}
    for key, holders in sorted(derived_holders.items()):
        if key not in phrases:
            continue
        for row in holders:
            if key in row.aliases or not seen(row):
                continue
            rivals = [
                other
                for other in rows
                if other.anchor_id != row.anchor_id
                and (
                    key in {normalize(name) for name in _authored_names(other)}
                    or _derived_key(other.title) == key
                )
            ]
            if any(seen(other) for other in rivals):
                continue
            key_terms = _counted_terms((key,))
            if key_terms and all(
                counts.get(term, 0) <= RARE_TERM_MAX_ANCHORS for term in key_terms
            ):
                restored[row.anchor_id] = (*row.aliases, key)

    kept = tuple(
        replace(row, aliases=restored[row.anchor_id]) if row.anchor_id in restored else row
        for row in rows
        if decided.get(row.anchor_id, True)
    )
    return kept, counts, frozenset(decided)


def _claims_winner(analysis: TurnAnalysis, routing_targets: Sequence[Any]) -> str | None:
    """Delegate to the existing claims router; never re-implement its rule."""
    if not routing_targets:
        return None
    from . import collection_claims

    decision = collection_claims.route(analysis.tokens, routing_targets)
    if not isinstance(decision, Mapping):
        return None
    collection = decision.get("collection")
    return str(collection) if isinstance(collection, str) else None


#: A spread below this is a population whose signatures are all alike (empty
#: or identical), which has no chance level to measure a turn against.
_BAND_MIN_SPREAD = 1e-6


def semantic_band(similarities: Any, *, alpha: float, min_population: int) -> frozenset[int] | None:
    """Rows whose similarity to the turn clears the chance maximum; None when
    the population cannot be calibrated.

    `m = median(s)`, `σ = 1.4826 · MAD(s)`, and `k(N, α) = Φ⁻¹((1-α)^(1/N))` is
    the level the largest of N unrelated similarities exceeds with probability
    α. Row i clears when `s_i ≥ m + k·σ`. The median and MAD are unmoved by the
    few rows a turn is really about, and the rule is invariant to adding a
    constant to every similarity or scaling them all, so no number in it
    belongs to one model, one language or one vault. Fewer than
    `min_population` rows, or a degenerate spread, has no chance level (design
    §5.1).
    """
    import numpy as np

    values = np.asarray(similarities, dtype="float64").reshape(-1)
    n = int(values.size)
    if n < max(1, int(min_population)) or not np.all(np.isfinite(values)):
        return None
    centre = float(np.median(values))
    spread = 1.4826 * float(np.median(np.abs(values - centre)))
    if spread < _BAND_MIN_SPREAD:
        return None
    level = statistics.NormalDist().inv_cdf(math.pow(1.0 - float(alpha), 1.0 / n))
    return frozenset(int(i) for i in np.flatnonzero(values >= centre + level * spread))


def vector_bands(
    vectors: Mapping[str, Any] | None,
    query_vector: Any,
    config: RankingConfig | None = None,
) -> tuple[dict[str, bool], str]:
    """`vector_band` per signature, and whether the population was calibrated.

    Every signature in `vectors` is the population the turn is measured
    against. If more than `RARE_TERM_MAX_ANCHORS` anchors clear, none bands: a
    turn similar to many anchors is about a topic, the same judgement that stops
    a word naming four anchors from being rare. Returns `({}, "uncalibrated")`
    when there is no chance level. The similarity never leaves this function.
    """
    if not vectors or query_vector is None:
        return {}, "ready"
    try:
        import numpy as np

        ids: list[str] = []
        rows: list[Any] = []
        width = np.asarray(query_vector).size
        for anchor_id, vector in vectors.items():
            candidate = np.asarray(vector, dtype="float32").reshape(-1)
            candidate_norm = float(np.linalg.norm(candidate))
            if candidate.size != width or candidate_norm == 0.0:
                continue  # a malformed row costs its band, nothing else
            ids.append(anchor_id)
            rows.append(candidate / candidate_norm)
        if not rows:
            return {}, "ready"
        matrix = np.vstack(rows)
    except Exception:  # noqa: BLE001 - the vector lane is optional by contract
        return {}, "ready"
    return matrix_bands(tuple(ids), matrix, query_vector, config)


def matrix_bands(
    ids: Sequence[str], matrix: Any, query_vector: Any, config: RankingConfig | None = None
) -> tuple[dict[str, bool], str]:
    """`vector_bands` over the index's cached, L2-normalised signature matrix:
    one matrix-vector product and a median, not a loop over rows."""
    config = config or DEFAULT_RANKING
    if matrix is None or not len(ids) or query_vector is None:
        return {}, "ready"
    try:
        import numpy as np

        query = np.asarray(query_vector, dtype="float32").reshape(-1)
        norm = float(np.linalg.norm(query))
        if norm == 0.0 or query.shape[0] != matrix.shape[1]:
            return {}, "ready"
        similarities = matrix @ (query / norm)
    except Exception:  # noqa: BLE001 - the vector lane is optional by contract
        return {}, "ready"
    cleared = semantic_band(
        similarities,
        alpha=float(config.working_set_semantic_alpha),
        min_population=int(config.working_set_semantic_min_population),
    )
    if cleared is None:
        return {}, "uncalibrated"
    if len(cleared) > RARE_TERM_MAX_ANCHORS:
        cleared = frozenset()
    return {anchor_id: index in cleared for index, anchor_id in enumerate(ids)}, "ready"


def _vector_bands(
    rows: Sequence[AnchorFacts],
    vectors: Mapping[str, Any] | None,
    query_vector: Any,
    config: RankingConfig,
) -> dict[str, bool]:
    """Band membership only (`vector_bands` without its state)."""
    return vector_bands(vectors, query_vector, config)[0]


def add_graph_corroboration(
    candidates: Sequence[CandidateFacts],
    *,
    retrieval_paths: frozenset[str] = frozenset(),
) -> tuple[CandidateFacts, ...]:
    """Add `graph_corroboration` to a candidate linked to an INDEPENDENTLY
    reached partner — one that carries a worded contact kind.

    The find lane discards graph corroboration for pages already in its primary
    set, because there it would double-count one page's own retrieval signal.
    Here the signal is about a DIFFERENT fact — that two candidates the turn
    reached are connected — so the discard would throw away the only evidence
    that distinguishes a coherent neighbourhood from two coincidences.

    "Independently reached" is the qualifier a link alone cannot supply: two
    candidates admitted through retrieved contact ALONE are linked by
    construction whenever they share a neighbourhood (a hub's own recall hit
    plus its members' hits, say), so a link between them restates the same
    ranking-engine fact rather than adding a second one. A partner that
    carries a worded contact kind — the turn's own words reached it — is the
    independent fact a link can legitimately corroborate.
    `retrieval_paths` is accepted so that intent is explicit at the call site.
    """
    del retrieval_paths  # deliberately unused: see the docstring.
    corroborated: set[str] = set()
    for item in candidates:
        for other in candidates:
            if other.anchor_id == item.anchor_id:
                continue
            linked = (other.path and other.path in item.neighbourhood) or (
                item.path and item.path in other.neighbourhood
            )
            if linked and (other.evidence & WORDED_CONTACT_KINDS):
                corroborated.add(item.anchor_id)
    return tuple(
        replace(item, evidence=item.evidence | {"graph_corroboration"})
        if item.anchor_id in corroborated
        else item
        for item in candidates
    )


def anchor_ref(row: Any) -> str:
    """The ref a packet reports for a row — the SAME expression `as_dict` uses.

    Continuity and the override both name anchors the way a previous packet
    spelled them, so the comparison has to be made against that spelling and
    not against the internal id. Spelling it once here keeps the two directions
    from drifting apart.
    """
    return str(
        getattr(row, "ref", None) or getattr(row, "path", "") or getattr(row, "anchor_id", "")
    )


def names_row(refs: frozenset[str] | set[str], row: Any) -> bool:
    """Does a continuity token's ref list name `row`?

    By the ref the packet reported (`anchor_ref`) or by the row's path. A
    token minted while a page had no identifier names it by path; once the
    page gains one (`backfill-ids`), its reported ref changes and the path is
    the only spelling the two still share. The path is the page, so matching
    it names nothing the token did not.
    """
    if not refs:
        return False
    path = str(getattr(row, "path", "") or "")
    return anchor_ref(row) in refs or (bool(path) and path in refs)


def apply_continuity(
    candidates: Sequence[CandidateFacts],
    refs: frozenset[str] | set[str],
) -> tuple[CandidateFacts, ...]:
    """Qualify the candidates a client-carried token names. Adds no candidate.

    This is the whole enforcement of "continuity never resolves alone": the
    function can only ever ADD a kind to an anchor the current turn already
    reached by a contact kind, because a candidate is what `candidates_for`
    produced and nothing here produces one. A ref naming an anchor this turn did
    not reach — or one the vault has since retired — simply matches nothing and
    is dropped without a word, since a hint that half-missed is still a hint.
    """
    if not refs:
        return tuple(candidates)
    return tuple(
        replace(item, evidence=item.evidence | {"continuity"}) if names_row(refs, item) else item
        for item in candidates
    )


def override_candidate(rows: Sequence[AnchorFacts], ref: str) -> CandidateFacts | None:
    """Compatibility view of the first candidate named by an agent choice."""
    chosen = override_candidates(rows, ref)
    return chosen[0] if chosen else None


def override_candidates(rows: Sequence[AnchorFacts], ref: str) -> tuple[CandidateFacts, ...]:
    """Exact identities named by the choice, including items in one canonical home.

    A collection path may represent several complementary Planning items. The
    choice selects all those items, never merely the first index row. An internal
    item id still selects only that item. No semantic neighbours are selected.

    `agent_choice` is its only evidence, and it resolves alone: the agent is the
    decider, so the turn's own evidence for that anchor is beside the point and
    the competing senses are not candidates at all. An empty tuple names no
    anchor in this index — the caller decides what to say about that, and by
    contract says exactly what it says about a withheld one.
    """
    wanted = str(ref or "").strip()
    if not wanted:
        return ()
    chosen: list[CandidateFacts] = []
    for row in rows:
        spellings = {anchor_ref(row), str(row.path or ""), str(row.anchor_id or "")}
        if wanted in spellings - {""}:
            chosen.append(
                CandidateFacts(
                    anchor_id=row.anchor_id,
                    path=row.path,
                    ref=row.ref,
                    title=row.title,
                    kind=row.kind,
                    lifecycle=row.lifecycle,
                    categories=row.categories,
                    neighbourhood=row.neighbourhood,
                    anchor_neighbourhood=row.anchor_neighbourhood,
                    evidence=frozenset({"agent_choice"}),
                )
            )
    return tuple(chosen)


# --------------------------------------------------------------------------- #
# The rule
# --------------------------------------------------------------------------- #


def _candidate_order(candidate: CandidateFacts) -> tuple:
    """Deterministic order: named anchors first, then deciding kinds, then
    the usage tie-break, then id.

    R5 (fix/activation-competing-senses): an anchor holding a deciding-alone
    kind (`exact_alias`/`agent_choice` — the turn spelled its own name, or
    the agent IS the decider) sorts before every anchor that does not, ahead
    of the existing keys. Without this, `-len(deciding_kinds)` alone could
    sort several weak two-kind candidates ahead of the one anchor the turn
    actually named, and `candidates_for`'s MAX_CANDIDATES / `resolve`'s
    MAX_ANCHORS truncation — both keyed by this SAME function — could drop
    it. `usage_prior` appears here and ONLY here — it orders otherwise-equal
    candidates and never changes a status.
    """
    return (
        0 if candidate.deciding_kinds & DECIDING_ALONE_KINDS else 1,
        # The prior is not counted: heat never reorders candidates the turn
        # named. Where a recency referent must survive a cut, `resolve()` and
        # `candidates_for` keep it explicitly instead.
        -len(candidate.deciding_kinds - PRIOR_CONTACT_KINDS),
        0 if "exact_alias" in candidate.evidence else 1,
        0 if "usage_prior" in candidate.evidence else 1,
        candidate.anchor_id,
    )


def _status_for_evidence(evidence: frozenset[str], *, recency_resolves: bool = False) -> str:
    """The three-clause soundness rule (design.md decision 1), plus continuity.

    `resolved` iff: `exact_alias` or `agent_choice` (either decides alone —
    the turn spelled the anchor's own name, or the agent IS the decider); or
    `lexical_overlap`/`claims_match` plus at least one other kind besides
    `usage_prior` (a qualifier is enough — the turn's own words already
    reached the anchor); or `rare_term` plus at least one other CONTACT kind
    specifically (a qualifier alone is not enough — the weak worded kind
    needs a second, independent fact, not merely a strengthener of itself);
    or `continuity` plus at least one CONTACT kind of either family (a
    previous packet's resolution plus this turn's own contact — `continuity`
    still never creates a candidate and never resolves alone or with
    qualifiers only). Retrieved contact alone, however many retrieved kinds
    and qualifiers co-occur, is never more than `partial`.

    Takes the evidence set directly, not a `CandidateFacts`/`ResolvedAnchor`,
    so R1's same-kind subsumption rule (`resolve`) can re-run this same
    clause against a candidate's evidence with `exact_alias` removed, to ask
    "would this anchor still resolve on its OTHER evidence alone" — without
    building a throwaway `CandidateFacts` just to hold a modified set.

    `recency_resolves` is the FIFTH clause (close-memory-loop D2), and it is
    the caller's answer to a question about the WHOLE candidate set, not
    about this evidence: is the turn referential, and did no candidate
    anywhere carry worded contact? `resolve()` is where that is visible and
    where it is computed; here it only opens the clause.

    `PRIOR_CONTACT_KINDS` leaves `deciding` along with the tie-breaks, so a
    prior can never be the second kind that promotes somebody else. That
    subtraction cannot change any evidence set that was expressible before
    `recency` existed, so the four clauses above are the four clauses that
    were there.
    """
    deciding = evidence - TIE_BREAK_KINDS - PRIOR_CONTACT_KINDS
    if deciding & DECIDING_ALONE_KINDS:
        return "resolved"
    if deciding & {"lexical_overlap", "claims_match"} and len(deciding) >= 2:
        return "resolved"
    if "rare_term" in deciding and (deciding & CONTACT_KINDS) - {"rare_term"}:
        return "resolved"
    if deciding & NAMED_BEFORE_KINDS and deciding & CONTACT_KINDS:
        return "resolved"
    if recency_resolves and "recency" in evidence:
        return "resolved"
    # A candidate the PRIOR admitted — hot, on a referential turn, with no
    # contact of its own — whose clause stayed shut because something else
    # was named. Its qualifiers (`continuity`, `category_match`) would make
    # `deciding` non-empty and list it `partial`, a menu entry whose only
    # claim is that somebody edited it. Before `recency` existed no such
    # candidate could be built, so this reads only sets that contain it.
    if evidence & PRIOR_CONTACT_KINDS and not evidence & CONTACT_KINDS:
        return "unresolved"
    if deciding:
        return "partial"
    # Nothing but tie-breaks: not a candidate this turn reached at all.
    return "unresolved"


def _status_for(candidate: CandidateFacts, *, recency_resolves: bool = False) -> str:
    """`_status_for_evidence`, applied to one candidate's own evidence."""
    return _status_for_evidence(candidate.evidence, recency_resolves=recency_resolves)


def _phrase_spans(tokens: Sequence[str], phrase_tokens: Sequence[str]) -> list[tuple[int, int]]:
    """Every contiguous span in `tokens` whose slice equals `phrase_tokens`,
    verbatim OR after `fold_possessive` (fix/activation-competing-senses,
    R4): the de-possessived and verbatim spellings of one turn token occupy
    the SAME position, so a phrase built from either reading still finds the
    turn's own "gamma's" when it goes looking for "gamma".

    A plain sliding-window scan, bounded by the turn's own length — never an
    index read. Shared by R1 (a phrase's occurrences in the raw turn) and R2
    (the token positions a multi-token `exact_alias` phrase covers).
    """
    width = len(phrase_tokens)
    if width == 0 or width > len(tokens):
        return []
    phrase = tuple(phrase_tokens)
    folded_phrase = tuple(fold_possessive(token) for token in phrase)
    spans = []
    for start in range(len(tokens) - width + 1):
        window = tuple(tokens[start : start + width])
        if window == phrase or tuple(fold_possessive(token) for token in window) == folded_phrase:
            spans.append((start, start + width))
    return spans


def _is_strict_subphrase(short: str, long: str) -> bool:
    """Is `short`'s own token sequence a proper, contiguous run inside `long`'s?

    Equality is excluded on purpose ("strict"): two anchors that earned the
    identical `exact_alias` phrase are not one subsuming the other — see
    `test_r1_identical_exact_alias_phrase_pair_stays_ambiguous`.
    """
    if short == long:
        return False
    short_tokens = short.split(" ")
    long_tokens = long.split(" ")
    if len(short_tokens) >= len(long_tokens):
        return False
    return bool(_phrase_spans(long_tokens, short_tokens))


def _has_free_standing_mention(
    shorter_phrases: frozenset[str], longer_phrases: frozenset[str], turn_tokens: Sequence[str]
) -> bool:
    """Does any occurrence of a shorter-anchor phrase fall outside every
    occurrence of a longer-anchor phrase in the RAW turn ("compare alpha
    hosted with alpha")? If so the shorter anchor is a free-standing mention
    of its own, not merely a fragment of the longer name, and R1 must not
    demote it.
    """
    longer_spans = [
        span for phrase in longer_phrases for span in _phrase_spans(turn_tokens, phrase.split(" "))
    ]
    for phrase in shorter_phrases:
        for start, end in _phrase_spans(turn_tokens, phrase.split(" ")):
            if not any(l_start <= start and end <= l_end for l_start, l_end in longer_spans):
                return True
    return False


def _demote_subsumed_same_kind_aliases(
    anchors: Sequence[ResolvedAnchor], turn_tokens: Sequence[str]
) -> tuple[ResolvedAnchor, ...]:
    """R1 (fix/activation-competing-senses): a shorter spelled name wholly
    inside a longer spelled name is a free rider on the longer mention, not a
    second competing sense. Turn spells "Dana Whitfield": person anchor
    "Dana Whitfield" resolves on `exact_alias`; person anchor "Dana" ALSO
    resolves on `exact_alias` (the turn phrase "dana" is the same word), same
    kind, no structural link — `_ambiguity` would otherwise report them as
    competing and the whole packet would abstain.

    Demotes anchor A (to `partial`) when, for some other RESOLVED anchor B of
    the SAME kind: every `exact_alias` phrase A earned is a strict contiguous
    token sub-phrase of some phrase B earned, AND A is resolved ONLY because
    of `exact_alias` (removing it and re-running the soundness rule on what
    remains still does not resolve). Cross-kind pairs are untouched — a
    product and a page named after it are complementary, the existing rule
    already says so, and `_ambiguity` never compares across kinds either.

    Position-aware exception: if A's phrase ALSO occurs in the turn at a
    token position not covered by any occurrence of B's longer phrase
    ("compare alpha hosted with alpha"), A is a free-standing mention of its
    own and is NOT demoted. This needs the raw turn tokens, which most
    existing `resolve()` callers never pass — `turn_tokens` empty simply
    means the exception can never fire, and the base subsumption rule alone
    decides (a documented default, not a silent behaviour change: see
    `test_r1_without_turn_tokens_the_free_standing_exception_is_unavailable`).
    """
    result = list(anchors)
    resolved_indices = [i for i, anchor in enumerate(result) if anchor.status == "resolved"]
    for i in resolved_indices:
        candidate = result[i]
        if not candidate.exact_alias_phrases:
            continue
        without_alias = frozenset(candidate.evidence) - {"exact_alias"}
        if _status_for_evidence(without_alias) == "resolved":
            continue
        for j in resolved_indices:
            if i == j:
                continue
            other = result[j]
            if other.kind != candidate.kind or not other.exact_alias_phrases:
                continue
            subsumed = all(
                any(
                    _is_strict_subphrase(phrase, longer)
                    for longer in other.exact_alias_phrases
                )
                for phrase in candidate.exact_alias_phrases
            )
            if not subsumed:
                continue
            if turn_tokens and _has_free_standing_mention(
                candidate.exact_alias_phrases, other.exact_alias_phrases, turn_tokens
            ):
                continue
            result[i] = replace(result[i], status="partial")
            break
    return tuple(result)


def resolve(
    candidates: Sequence[CandidateFacts],
    *,
    turn_tokens: Sequence[str] = (),
    referential: bool = False,
) -> Resolution:
    """Derive anchor statuses and the turn's verdict from categorical evidence.

    `turn_tokens` is the raw turn's own tokens (`TurnAnalysis.tokens`), used
    only by R1's position-aware free-standing-mention exception; production
    callers pass `analysis.tokens`, and omitting it (as most direct unit-test
    callers do) simply leaves that exception unavailable.

    `referential` is `TurnAnalysis.referential`: the turn pointed at recent
    work instead of naming any. It is half of the fifth soundness clause; the
    other half is decided here, because it is a fact about the whole set —
    a prior may supply a referent only where NO candidate anywhere carries
    worded contact. One candidate the turn actually named, of any strength,
    and recency is back to reporting a fact about the vault and deciding
    nothing. Omitting it (every direct unit-test caller that has no opinion)
    leaves the clause shut.
    """
    for candidate in candidates:
        unknown = sorted(candidate.evidence - frozenset(EVIDENCE_KINDS))
        if unknown:
            raise ValueError(f"unknown activation evidence kind: {unknown[0]}")
    recency_resolves = referential and not any(
        candidate.evidence & WORDED_CONTACT_KINDS for candidate in candidates
    )
    ordered = sorted(candidates, key=_candidate_order)
    anchors: list[ResolvedAnchor] = []
    for candidate in ordered:
        status = _status_for(candidate, recency_resolves=recency_resolves)
        if status == "unresolved":
            continue
        anchors.append(
            ResolvedAnchor(
                anchor_id=candidate.anchor_id,
                path=candidate.path,
                ref=candidate.ref,
                title=candidate.title,
                kind=candidate.kind,
                lifecycle=candidate.lifecycle,
                status=status,
                evidence=tuple(sorted(candidate.evidence)),
                categories=candidate.categories,
                neighbourhood=candidate.neighbourhood,
                anchor_neighbourhood=candidate.anchor_neighbourhood,
                exact_alias_phrases=candidate.exact_alias_phrases,
                name_contact=candidate.name_contact,
                name_span=candidate.name_span,
                entity_type=candidate.entity_type,
                name_capitalised=candidate.name_capitalised,
                name_lower_case=candidate.name_lower_case,
            )
        )
    if recency_resolves:
        # A recency referent carries no contact kind, so the ordinary order
        # puts it behind every partial the turn's filler words reached, and the
        # cut below dropped it. Where the fifth clause is open, resolved
        # anchors go first — stably, so each group keeps its order. Every
        # other resolution is cut exactly as before.
        anchors.sort(key=lambda item: 0 if item.status == "resolved" else 1)
    anchors = anchors[:MAX_ANCHORS]
    anchors = _demote_subsumed_same_kind_aliases(anchors, turn_tokens)
    anchors = _narrowed_by_qualifier(anchors)
    resolved = [anchor for anchor in anchors if anchor.status == "resolved"]
    groups = _competing_groups(resolved)
    # R3 (fix/activation-competing-senses): a named anchor carries the
    # packet. Gated on whether SOME resolved anchor anywhere holds a
    # deciding-alone kind (`exact_alias`/`agent_choice`) — with none
    # anywhere, behaviour is unchanged (the agent may still be asked to
    # choose between two weak senses, exactly as today).
    has_named_anchor = any(DECIDING_ALONE_KINDS & set(anchor.evidence) for anchor in resolved)
    ambiguity: list[dict[str, Any]] = []
    demoted_refs: set[str] = set()
    disambiguated_by = ""
    for kind, group in groups:
        group_has_named = any(DECIDING_ALONE_KINDS & set(member.evidence) for member in group)
        if group_has_named or not has_named_anchor:
            # A real competing sense (a group the turn itself named two
            # members of), or -- with no named anchor anywhere to carry the
            # packet instead -- today's unchanged behaviour. The earlier
            # conversation breaks the tie only when it is unambiguous:
            # exactly one competitor carries it. None or several keep the
            # ambiguity exactly as without a conversation.
            named_before = {
                anchor_ref(member) for member in group if "conversation" in member.evidence
            }
            if len(named_before) == 1:
                demoted_refs.update(anchor_ref(member) for member in group)
                demoted_refs.difference_update(named_before)
                disambiguated_by = "conversation"
            else:
                ambiguity.extend(_ambiguity_dicts(kind, group))
        else:
            # None of this group's members is a named anchor, and a named
            # anchor exists elsewhere to carry the packet: demote the whole
            # weak, unlinked group to `partial` rather than aborting the
            # turn on a competition the named anchor makes irrelevant.
            demoted_refs.update(anchor_ref(member) for member in group)
    if demoted_refs:
        anchors = tuple(
            replace(anchor, status="partial")
            if anchor.status == "resolved" and anchor_ref(anchor) in demoted_refs
            else anchor
            for anchor in anchors
        )
        resolved = [anchor for anchor in anchors if anchor.status == "resolved"]
    if ambiguity:
        return Resolution(status="ambiguous", anchors=tuple(anchors), ambiguity=tuple(ambiguity))
    if resolved:
        return Resolution(
            status="resolved", anchors=tuple(anchors), disambiguated_by=disambiguated_by
        )
    bare = [
        entry
        for kind, group in _bare_name_groups(anchors)
        for entry in _ambiguity_dicts(kind, group)
    ]
    if bare:
        return Resolution(status="ambiguous", anchors=tuple(anchors), ambiguity=tuple(bare))
    return Resolution(status="unresolved", anchors=tuple(anchors))


def _span_inside(inner: tuple[int, int] | None, outer: tuple[int, int] | None) -> bool:
    """Is turn span `inner` strictly inside `outer`?"""
    if inner is None or outer is None or inner == outer:
        return False
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _narrowed_by_qualifier(anchors: Sequence[ResolvedAnchor]) -> tuple[ResolvedAnchor, ...]:
    """A qualifier narrows competing senses (close-memory-loop, activation
    quality).

    Each anchor's contact is the longest contiguous run of the turn that
    spells its own name words (`name_span`). Two same-kind anchors both
    resolved, and the run spelling one lies strictly inside the run spelling
    the other: the turn said the shared words AND, in the same breath, a word
    only the second one's name carries. "The tide model rollout" named the
    rollout hub, not the research hub that "the tide model" alone would leave
    open. The narrower sense is not listed at all; and, for that kind,
    neither is a `partial` anchor the turn reached only inside that run,
    which is a free rider on that mention exactly as R2's consumed
    `rare_term` is.

    A word of the wider name said ELSEWHERE in the turn ("I blew my grocery
    budget, and the kitchen renovation is stalled") leaves both runs the same
    and narrows nothing; nor does a turn that names each sense in its own
    run. With no strict containment the turn is left as it was, and an
    ambiguous turn keeps every sense its words touched as the agent's menu. A
    sense the turn spelled by name (`exact_alias`, `agent_choice`) is never
    narrowed out: R1 already decides between spelled names. Cross-kind
    anchors are complementary and never narrow one another.
    """
    resolved = [
        anchor
        for anchor in anchors
        if anchor.status == "resolved" and anchor.name_span is not None
    ]
    narrowed: dict[str, set[str]] = {}
    chosen: dict[str, list[ResolvedAnchor]] = {}
    for anchor in resolved:
        if DECIDING_ALONE_KINDS & set(anchor.evidence):
            continue
        wider = [
            other
            for other in resolved
            if other.kind == anchor.kind and _span_inside(anchor.name_span, other.name_span)
        ]
        if wider:
            narrowed.setdefault(anchor.kind, set()).add(anchor.anchor_id)
            chosen.setdefault(anchor.kind, []).extend(wider)
    if not narrowed:
        return tuple(anchors)

    def dropped(anchor: ResolvedAnchor) -> bool:
        excluded = narrowed.get(anchor.kind)
        if not excluded:
            return False
        if anchor.anchor_id in excluded:
            return True
        return (
            anchor.status == "partial"
            and not set(anchor.evidence) & (CONTACT_KINDS - {"rare_term", "lexical_overlap"})
            and any(_span_inside(anchor.name_span, wide.name_span) for wide in chosen[anchor.kind])
        )

    return tuple(anchor for anchor in anchors if not dropped(anchor))


#: Anchor kinds a bare name can refer to. A person's or an organisation's
#: name is how it is spoken of; a word two hub or resource titles share is an
#: ordinary noun, and saying it names neither.
BARE_NAME_KINDS: frozenset[str] = frozenset({"entity"})
#: The entity type whose shared name is a name however the turn writes it.
PERSON_ENTITY_TYPE = "person"


def _spoken_as_name(term: str, members: Sequence[ResolvedAnchor]) -> bool:
    """Was the shared word `term` said as a NAME, not as an ordinary word?

    Every member a person: a first name is a name however it is written, in
    any script, unless the turn's casing says something (some words
    capitalised, some not) and it wrote this word in lower case, so "please
    mark the task done" never asks between two people named Mark, while an
    all-lower-case turn ("priya sent it") still does. Otherwise only a cased script can tell, and only by a capital
    the turn gave the word away from a sentence start ("we ordered from
    Harbour again", not "the harbour was busy" or "Harbour traffic was
    heavy"). An uncased script (CJK) carries no such mark, so there a shared
    word forms the group for people only.
    """
    if all(member.entity_type == PERSON_ENTITY_TYPE for member in members):
        return not any(member.name_lower_case for member in members)
    return _is_cased(term) and any(member.name_capitalised for member in members)


def _bare_name_groups(
    anchors: Sequence[ResolvedAnchor],
) -> tuple[tuple[str, tuple[ResolvedAnchor, ...]], ...]:
    """Unlinked entities one bare shared name reached, when nothing resolved.

    Each member is `partial` on that one name word alone (`rare_term`, plus
    qualifiers at most): the turn said a first name, say "Priya", and two
    people are called Priya. When the word was said as a name
    (`_spoken_as_name`), that is the question competing senses exist to ask,
    so the turn is `ambiguous` between them, formed by the same connectivity
    rule every competing group uses: two people who link each other are one
    neighbourhood, not two senses. One entity alone stays a `partial` lead.
    """
    by_name: dict[tuple[str, str], list[ResolvedAnchor]] = {}
    for anchor in anchors:
        if (
            anchor.status != "partial"
            or anchor.kind not in BARE_NAME_KINDS
            or len(anchor.name_contact) != 1
            or set(anchor.evidence) & CONTACT_KINDS != {"rare_term"}
        ):
            continue
        (term,) = anchor.name_contact
        by_name.setdefault((anchor.kind, term), []).append(anchor)
    groups: list[tuple[str, tuple[ResolvedAnchor, ...]]] = []
    for (_kind, term), members in sorted(by_name.items()):
        if len(members) >= 2 and _spoken_as_name(term, members):
            groups.extend(_competing_groups(members))
    return tuple(groups)


def _competing_groups(
    resolved: Sequence[ResolvedAnchor],
) -> tuple[tuple[str, tuple[ResolvedAnchor, ...]], ...]:
    """Disconnected groups of resolved anchors of ONE kind that compete.

    Restricted to a single anchor kind deliberately. A person and a product
    resolved by the same turn are complementary — that is the whole point of a
    cross-cutting packet — and treating them as competing senses would abstain
    on nearly every useful turn. Competing SENSES are same-kind by construction:
    two hubs, two resources, two people.

    Disjointness is evaluated over each anchor's ANCHOR neighbourhood — the
    neighbours that are themselves anchors in the activation index — because
    complementarity is a claim about structure. A shared page that is not an
    anchor is boilerplate, a navigation stub, or a page one hub happened to reach
    through an alias spelling; it says nothing about whether two senses belong
    together, and letting it bridge them would silently suppress the abstention.
    Two anchors are also related when one IS the other's anchor neighbour, in
    either direction: a direct typed link is the strongest relatedness the graph
    can express, and link direction is an authoring accident. Without that half
    of the test the rule abstains on exactly the turns the packet exists to
    serve — two entities the user asks to compare, which link to each other and
    so both carry `graph_corroboration` from that very edge.

    Distinct items in the same canonical page or collection are complementary
    too: an outcome and its next action need no extra edge to establish their
    shared home. Empty paths never establish that relationship. Compare connected
    groups, so a complementary pair cannot hide a third, disjoint competitor.

    `project` anchors come from project keys rather than from a page, so their
    path is empty and no neighbourhood can contain them: they can neither bridge
    two anchors nor be anyone's neighbour, so two resolved project anchors are
    trivially disjoint and are reported as competing, which is the right outcome
    for two keys with no structure to judge them by.

    Examines EVERY kind (fix/activation-competing-senses, R3): the original
    returned on the FIRST kind it found a disconnected group in, so a
    demotable weak group of one kind could hide a real ambiguity of another
    kind the loop never reached, and vice versa. `resolve` decides, per
    returned group, whether it is real ambiguity or an R3 demotion.
    """
    groups: list[tuple[str, tuple[ResolvedAnchor, ...]]] = []
    for kind in sorted({anchor.kind for anchor in resolved}):
        group = [anchor for anchor in resolved if anchor.kind == kind]
        if len(group) < 2:
            continue
        # At most MAX_ANCHORS nodes; a bounded structural connectivity check.
        reached = {0}
        pending = [0]
        while pending:
            anchor = group[pending.pop()]
            for index, other in enumerate(group):
                if index in reached:
                    continue
                if (
                    (anchor.path and anchor.path == other.path)
                    or (anchor.anchor_neighbourhood & other.anchor_neighbourhood)
                    or other.path in anchor.anchor_neighbourhood
                    or anchor.path in other.anchor_neighbourhood
                ):
                    reached.add(index)
                    pending.append(index)
        if len(reached) != len(group):
            groups.append((kind, tuple(group)))
    return tuple(groups)


def _ambiguity_dicts(kind: str, group: Sequence[ResolvedAnchor]) -> tuple[dict[str, Any], ...]:
    """One competing group's own `ambiguity` block entries.

    A canonical path is one agent choice even when several Planning items
    inhabit it. Preserve each item's title in that choice. The reported
    `neighbourhood_size` stays the FULL one: the brain is being told how
    large each neighbourhood is, not how the rule was evaluated.
    """
    choices: dict[str, list[ResolvedAnchor]] = {}
    for anchor in group:
        choices.setdefault(anchor_ref(anchor), []).append(anchor)
    return tuple(
        {
            "ref": ref,
            "title": "; ".join(dict.fromkeys(item.title for item in members)),
            "kind": kind,
            "neighbourhood_size": len(
                frozenset().union(*(item.neighbourhood for item in members))
            ),
        }
        for ref, members in choices.items()
    )


def facts_from_rows(rows: Iterable[Any]) -> tuple[AnchorFacts, ...]:
    """Project `working_set_index.AnchorRow` values onto resolver facts."""
    return tuple(
        AnchorFacts(
            anchor_id=row.anchor_id,
            path=row.path,
            ref=row.ref,
            title=row.title,
            kind=row.kind,
            lifecycle=row.lifecycle,
            aliases=row.aliases,
            terms=row.terms,
            categories=row.categories,
            neighbourhood=row.neighbourhood,
            anchor_neighbourhood=row.anchor_neighbourhood,
            entity_type=str(getattr(row, "entity_type", "") or ""),
        )
        for row in rows
    )
