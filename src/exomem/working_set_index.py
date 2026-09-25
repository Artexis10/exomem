"""The activation index: a disposable anchor sidecar over governed structure.

What it is for. `activate_context` accepts a raw turn and has to decide, in
bounded work, which durable anchors that turn is about. Ordinary recall cannot
answer that: it ranks pages by resemblance to a query, and a turn is not a
query. So the compiler needs a small, structural catalogue of the things a turn
can be *about* — entities and their aliases, hubs, the `Products/` and
`Systems/` pages that name resources, active Planning items, Records collection
manifests and their `claims`, and the project keys — with the alias and lexical
terms that let a turn reach them.

What it is NOT. It is never authoritative and never enters an ordinary recall
lane: every row is derived from the vault and the vault alone, and deleting the
file costs nothing but a rebuild. Nothing here is generated: a signature is the
page's own title, lede and headline section names, or a manifest's own field
names and claims. The constitution forbids a server-side generative model, and
there is none in this file.

Conventions mirrored from `embedding_index.py`, deliberately and for the same
reasons: a `meta(key, value)` write-generation token bumped inside the write
transaction (WAL commits do not move the file's mtime, so mtime-keyed
invalidation both spuriously misses and goes stale), a copy-on-write in-process
row cache keyed on that token, and a scoped wipe when the schema version on
disk is not the one this binary writes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import find_corpus, sidecar_store
from .kbdir import kb_dirname
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

#: Schema version of the sidecar this binary writes. A mismatch wipes.
#: v2 added `page_names`: an index without it cannot tell a dangling wikilink
#: from an unresolved one, so it must rebuild rather than answer from silence.
#: v3 keyed it on `(name, path)`: one row per name dropped every page but one
#: whose stem normalised the same way, leaving the loser undecided.
#: v4 added frontmatter `aliases` to the indexed spellings: `[[Dossier]]` is a
#: working vault link, so a name map without it left an alias-spelled reference
#: unresolvable and therefore undecidable.
#: v5 (`make-anchor-resolution-sound`) added the title/alias term->anchor-count
#: table `rare_term` rarity is measured against, and derived short names (a
#: title's leading name before a trailing parenthetical or dash qualifier) as
#: aliases while unique in the catalogue: an index without either cannot tell a
#: rare word from a common one or resolve a natural short reference at all.
#: v6 (same change, independent review round 3) corrects what v5 actually
#: computed: the term-count table and the derived-name gates now cover every
#: anchor kind (not pages alone) and respect the `MAX_ANCHORS` cap
#: consistently, and a derived name is validated (no filename-like, digits-
#: only, stopwords-only, four-or-more-word, or under-three-character lead). A
#: v5 sidecar's stored aliases and counts were computed by the pre-fix rules
#: and would otherwise survive unrebuilt until an unrelated vault edit.
#: v7 (task 4a, found after merge) replaced the ASCII-only `[a-z0-9]`
#: tokeniser with a Unicode-aware one: a term is now a maximal run of
#: letters, digits and combining marks in any script, not just basic Latin.
#: `normalize()` — the one fold site every lexical comparison key in this
#: module shares, including `resolve_names`'s egress-guard lookups — folds
#: a typographic apostrophe (`’`) and a typographic or non-breaking hyphen
#: to the plain one, drops a soft hyphen, and a derived short name is no
#: longer admitted when it CONTAINS A WORD longer than 48 code points (not
#: when the joined name is — three ordinary compound words are still a
#: name). A v6 sidecar's title/alias
#: terms, derived short names and term->anchor counts were all computed by
#: the fragmenting, non-Latin-blind, quote- and hyphen-splitting rule (an
#: accented word split into fragments, a non-Latin script produced no terms
#: at all, "i'd" and "i’d" tokenised as different words, "well-known" and
#: "well‑known" did too) and must rebuild rather than answer from those
#: stale rows.
#: v8 (fix/activation-competing-senses, correction round 1) adds words to
#: `STOPWORDS`: "let" (C1) and the closed-class function words C3 adds
#: (before/after/into/etc. — see that commit for the full list).
#: `derived_short_name`'s stopwords-only rejection reads `STOPWORDS`
#: directly and runs at index-build time (`_finalize_anchor_aliases`), so a
#: v7 sidecar's derived short names were computed against the SMALLER word
#: list and would otherwise survive unrebuilt: a title whose leading name is
#: now entirely closed-class words (implausible for "let" alone, more
#: plausible once C3's larger list lands) would keep a derived alias a fresh
#: build would refuse to derive. `term_anchor_counts` is unaffected —
#: `_title_alias_term_owners` never filters by `STOPWORDS` — but the bump
#: covers both commits in this round since C3 needs one for the SAME table
#: and a schema version is an all-or-nothing per-round bump, not a per-word
#: one.
#: v9 (step 4, T7): signature vectors are stored with the digest of the
#: signature they embed, and the sidecar records the encoder fingerprint they
#: were made with (`index_meta`). A v8 sidecar's vectors name no encoder, so
#: they are wiped rather than read under whatever encoder is resident now.
SCHEMA_VERSION = 9
SIDECAR_NAME = ".working-set.sqlite"
DISABLE_ENV = "EXOMEM_DISABLE_WORKING_SET"
_TRUE = frozenset({"1", "true", "yes", "on"})

#: Anchor kinds, in resolution-priority order. `entity` is the referents stage's
#: kind; the other five are what this change generalises it to.
ANCHOR_KINDS: tuple[str, ...] = (
    "entity",
    "resource",
    "hub",
    "collection",
    "plan",
    "project",
)

#: Caps. The index is a catalogue of *anchors*, not of notes: a vault with more
#: hubs than this has a structure problem the index should not paper over, and
#: the bound is what keeps a cold build inside an unmanaged request budget.
MAX_ANCHORS = 2000
#: Signatures an inline (request-thread) index update may encode, with an
#: already resident encoder. A background pass has no bound. The rest stay
#: vectorless until a later pass, which costs them `vector_band`, not a request.
INLINE_VECTOR_ENCODE_LIMIT = 64
MAX_PLAN_ITEMS = 200
MAX_LINKS_PER_ANCHOR = 40
SIGNATURE_MAX_CHARS = 600
LEDE_MAX_CHARS = 240

_NAVIGATION_BASENAMES = frozenset({"index.md", "log.md", "readme.md"})
#: Knowledge-base folders holding immutable raw material rather than anchors.
_RAW_MATERIAL_FOLDERS = frozenset({"Sources", "Evidence"})
_SKIP_DIR_NAMES = frozenset({"_trash", "_attachments", "_Staging", "Templates"})
_WIKILINK = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]")
_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)

#: The FAST PATH: basic-Latin terms only. Kept byte-for-byte as it always
#: was — every ASCII turn and title must tokenise exactly as it did before
#: task 4a — and used only when the whole normalised string `.isascii()`.
#: Non-ASCII text (a turn or a title with even one letter outside basic
#: Latin) instead goes through `_unicode_tokens`, below. Its `’` is now
#: unreachable — `normalize()` folds it to `'` before this pattern ever
#: runs — and is kept anyway so the pattern itself stays byte-identical to
#: the original regex, never separately maintained.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9'’\-]*")

#: The typographic (curly) right single quote. `normalize()` folds it to the
#: plain ASCII apostrophe, so "i'd" and "i’d" are the same word EVERYWHERE a
#: working-set comparison key is built from `normalize()` — a turn's tokens
#: (via `tokens_of`), an anchor's title and stored aliases, a derived short
#: name, a wikilink spelling. NFKC does not fold it on its own (both are
#: valid, distinct codepoints to Unicode), and folding it in only one of
#: those call sites (`tokens_of` alone, say) would leave a title or alias
#: AUTHORED with the typographic quote unable to ever earn `exact_alias`:
#: the turn's side and the anchor's side would each be folding, or not,
#: independently, on the exact defect this constant exists to close.
_TYPOGRAPHIC_APOSTROPHE = "’"

#: HYPHEN (U+2010) and SOFT HYPHEN (U+00AD), folded/dropped by `normalize()`
#: for the same reason as the apostrophe above: both sides of a comparison
#: must agree. NFKC already maps NON-BREAKING HYPHEN (U+2011) to U+2010, so
#: folding U+2010 alone covers both. En dash and em dash are deliberately
#: NOT folded here: a title uses one of those, not a hyphen, as its
#: qualifier separator (`derived_short_name`'s `_TRAILING_DASH`), and
#: folding them would make a qualifier separator indistinguishable from a
#: hyphenated word.
_TYPOGRAPHIC_HYPHEN = "‐"
_SOFT_HYPHEN = "­"

#: Continuation punctuation a term may carry after its first character — an
#: apostrophe or a hyphen — mirroring the fast path's `[a-z0-9'’\-]*` tail so
#: a term built by either path reads the same shape. No `’` here: by the
#: time text reaches this scanner, `tokens_of` has already folded it to `'`.
_TOKEN_JOINERS = frozenset({"'", "-"})


def _is_term_start(category: str) -> bool:
    """A term's first character: a letter or a number, never a mark or a
    punctuation mark — a combining mark has no base of its own to start a
    term with, per the spec's "the first character must be a letter or a
    number"."""
    return category[0] in ("L", "N")


def _is_term_continuation(category: str) -> bool:
    """A term's later characters: a letter, a number, or a combining mark.

    `\\w` is not a substitute for this: it excludes spacing combining marks,
    so a Devanagari vowel sign would still split its base letter from the
    rest of the word even under `\\w`.
    """
    return category[0] in ("L", "N", "M")


def _unicode_tokens(text: str) -> tuple[str, ...]:
    """The SLOW PATH: an explicit maximal-run scanner over
    `unicodedata.category`, for text the ASCII fast path cannot handle.

    Python's `re` module has no `\\p{L}` / `\\p{M}` Unicode property classes
    — that needs the third-party `regex` package, which this project does
    not depend on — so a term (the spec's "maximal run of letters, digits
    and combining marks in any script") is scanned character by character
    instead. Called only from `tokens_of`, and only for text that already
    failed `str.isascii()`.
    """
    tokens: list[str] = []
    current: list[str] = []
    for character in text:
        category = unicodedata.category(character)
        if current:
            if _is_term_continuation(category) or character in _TOKEN_JOINERS:
                current.append(character)
                continue
            tokens.append("".join(current))
            current = []
        if _is_term_start(category):
            current.append(character)
    if current:
        tokens.append("".join(current))
    return tuple(tokens)


#: Section/tag names that structurally imply a semantic-unit category. The map
#: is the shipped core category vocabulary plus its plural section spellings; it
#: is a lookup, not an inference, so the same page always yields the same set.
_CATEGORY_BY_LABEL: Mapping[str, str] = {
    "decision": "decision",
    "decisions": "decision",
    "fact": "fact",
    "facts": "fact",
    "finding": "finding",
    "findings": "finding",
    "insight": "insight",
    "insights": "insight",
    "constraint": "constraint",
    "constraints": "constraint",
    "requirement": "requirement",
    "requirements": "requirement",
    "assumption": "assumption",
    "assumptions": "assumption",
    "risk": "risk",
    "risks": "risk",
    "problem": "problem",
    "problems": "problem",
    "question": "question",
    "questions": "question",
    "open question": "question",
    "open questions": "question",
    "action": "action",
    "actions": "action",
    "next steps": "action",
    "technique": "technique",
    "techniques": "technique",
    "method": "technique",
    "methods": "technique",
    "preference": "preference",
    "preferences": "preference",
    "code": "code",
    "design": "design",
    "designs": "design",
    "config": "config",
    "configs": "config",
    "configuration": "config",
    "current state": "fact",
    "status": "fact",
    "summary": "fact",
}


def disabled() -> bool:
    """True when the kill switch forbids opening or creating the sidecar."""
    return os.environ.get(DISABLE_ENV, "").strip().casefold() in _TRUE


def sidecar_path(vault_root: Path) -> Path:
    """Where one vault's activation sidecar lives — machine-local, never in the vault."""
    return vault_state_dir(Path(vault_root)) / SIDECAR_NAME


def normalize(value: object) -> str:
    """NFKC + casefold + the typographic-apostrophe and -hyphen folds: the
    ONE normalisation every LEXICAL comparison key in this module shares.

    This is the single fold site (correction round, task 4a): every caller
    that builds a comparison key from an anchor's title or stored aliases,
    a turn's tokens, a derived short name, or a wikilink spelling goes
    through this function, so a typographic apostrophe or hyphen reads as
    the plain one on BOTH sides of every comparison, not just the turn's.
    Folding an apostrophe only where a turn is tokenised once left a title
    or alias itself AUTHORED with a typographic apostrophe unable to ever
    earn `exact_alias` — and the egress guard relies on this exact function
    too (`governance/egress._resolved_prose_names`, `working_set_index.
    resolve_names`), so a title and a prose wikilink naming it disagreeing
    on apostrophe or hyphen style once let a withheld page's own unit be
    served in full: the folds have to live here, not in a caller.

    "Every lexical comparison key in this module" is deliberately narrower
    than "every working-set comparison key": `collection_claims.
    normalize_text` and `structure_promotion._terms` (used for
    `claims_match` and Records current-state routing) keep their own,
    separate basic-Latin term splitter and do not call this function, so
    neither is reached yet by a non-Latin turn or a typographic apostrophe —
    a recorded, deliberate limit (see `SCHEMA_VERSION`'s v7 note), not an
    oversight, and its own change to lift.

    Locale-specific case rules are never applied: `str.casefold()` treats a
    Turkish dotted capital İ and a plain I as different letters, which is
    the correct behaviour for a vault with no locale of its own to assume.
    """
    return (
        unicodedata.normalize("NFKC", str(value))
        .strip()
        .casefold()
        .replace(_TYPOGRAPHIC_APOSTROPHE, "'")
        .replace(_TYPOGRAPHIC_HYPHEN, "-")
        .replace(_SOFT_HYPHEN, "")
    )


def tokens_of(text: str) -> tuple[str, ...]:
    """NFKC-casefolded word tokens in reading order, repetitions kept.

    Order and repetition matter to anything that slides a window over a turn:
    dropping the second `initiative` of "Alpha Initiative and Beta Initiative"
    destroys the phrase "beta initiative" entirely. Callers that want a term SET
    use `terms_of`.

    A term is a maximal run of letters, digits and combining marks in any
    script, which may carry an apostrophe or a hyphen after its first
    character; the first character itself must be a letter or a number.
    `normalize()` already folds a typographic apostrophe to the plain one
    (the single fold site — see its docstring), so "i'd" and "i’d" tokenise
    identically without this function doing anything of its own for it.
    Basic Latin text (once normalised) takes the FAST PATH — the original
    compiled regex, unchanged, so ASCII tokenisation is byte-identical to
    before task 4a; anything else takes the explicit Unicode scanner in
    `_unicode_tokens`, because a non-Latin letter must never split a word
    and a script the basic Latin alphabet does not cover must still yield
    terms.
    """
    normalized = normalize(text)
    if normalized.isascii():
        return tuple(match.group(0) for match in _TOKEN.finditer(normalized))
    return _unicode_tokens(normalized)


def terms_of(text: str) -> tuple[str, ...]:
    """Deterministic lexical terms: NFKC-casefolded word tokens, deduplicated."""
    return tuple(dict.fromkeys(tokens_of(text)))


#: A shared term this rare in the catalogue's title/alias vocabulary is a weak
#: worded contact on its own account (`working_set_resolve.rare_term`), and a
#: derived short name whose OWN terms are this rare is a genuine identifying
#: name rather than a common topic prefix (see `_derived_name_is_rare`).
#: Lives here, not in `working_set_resolve`, because both the resolver and
#: `_finalize_anchor_aliases` need it and this module has no dependency on
#: the resolver. Shipped default, judged against one vault (design.md: 622 of
#: 650 title terms there name at most three anchors) — moves into the
#: vault-owned conventions registry with `make-activation-conventions-vault-owned`.
RARE_TERM_MAX_ANCHORS = 3


#: A genuine "sibilant stem + `-es`" plural (class -> classes, box -> boxes,
#: alias -> aliases, bus -> buses) checked on the WHOLE word's trailing
#: letters. `fold_plural`'s docstring explains why this is a whole-word check
#: rather than a "stem after removing -es" check, and the one required pair it
#: cannot also satisfy (release/releases).
_SIBILANT_ES_SUFFIXES = ("ses", "xes", "zes", "ches", "shes")


def _fold_plural_once(term: str) -> str:
    if len(term) <= 3:
        return term
    if len(term) > 4 and term.endswith("ies"):
        return term[:-3] + "y"
    if term.endswith(_SIBILANT_ES_SUFFIXES):
        return term[:-2]
    if term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    return term


def fold_plural(term: str) -> str:
    """Fold a regular plural to the same canonical spelling as its singular.

    Approximate on purpose: a table of every English exception would be a
    second grammar engine, and the resolver only needs a turn's "posts" and an
    anchor's "post" to land on one canonical form for lexical comparison, not a
    linguistically perfect lemma. `exact_alias` is never folded — it compares
    the turn's own phrases against the anchor's own names verbatim.

    Rules, applied for up to two passes so "aliases" and "alias" meet: never
    fold a word of three characters or fewer; fold `-ies` (longer than four
    characters) to `-y`; strip `-es` only when the word ends `-ses`, `-xes`,
    `-zes`, `-ches` or `-shes` (class, box, alias, bus, process); otherwise
    strip one trailing `s` unless the word ends in `ss`, `us` or `is` (status,
    analysis, class, gas, bus, its, has are none of these three characters or
    fewer, or excluded, and so never change).

    Known residual collisions, accepted rather than hidden: "news"/"new",
    "means"/"mean", "lens"/"len" each fold to a shorter word that is not
    their singular. Two further CLASSES, found while implementing this rule:

    1. A singular ending in a silent `-se` never triggers either rule (no
       trailing `s` alone), while its plural ends in the word-final letters
       `-ses` — indistinguishable BY SUFFIX ALONE from a genuine `-ses`-ending
       plural of an `s`-final singular ("alias" + "es" = "aliases" ends in the
       identical `-ses` shape as "release" + "s" = "releases"). Folding
       `-ses` as that latter, `s`-final-singular shape (this function's
       choice, so "alias"/"aliases" and "class"/"classes" fold correctly,
       which the shipped requirement names explicitly) makes every `-se`
       singular collide with its own plural instead of matching it: release,
       case, base, use, phase, response, database, license, increase,
       purchase, house — none folds together with its plural. No suffix rule
       can satisfy both shapes, because both wordforms produce the same
       trailing letters once pluralised.
    2. A Greek-derived singular ending in `-is` is excluded from the
       trailing-`s`-strip rule on purpose (so "analysis", "basis", "crisis",
       "thesis" stay whole), but its irregular plural ends in `-es` and often
       lands on the `-ses` suffix rule instead: analysis/analyses,
       basis/bases, crisis/crises, thesis/theses — none of these four pairs
       folds together either. A different, unrelated irregularity from (1):
       here the singular is untouched and the PLURAL is stripped as if it
       were an `s`-final singular's `-es` plural.
    """
    folded = term
    for _ in range(2):
        next_folded = _fold_plural_once(folded)
        if next_folded == folded:
            break
        folded = next_folded
    return folded


def fold_possessive(token: str) -> str:
    """Strip a trailing possessive `'s` or a bare trailing `'` from one word
    (fix/activation-competing-senses, R4): "gamma's" and "gamma'" both fold
    to "gamma", so a possessive turn token can reach a plainly-named anchor
    the same way a plural turn token already reaches a singularly-named one
    via `fold_plural`.

    Called on text already through `normalize()`, which already folds a
    typographic apostrophe to the plain ASCII one — this function only ever
    strips a trailing `'s`/`'`, never detects a curly quote of its own.

    Mirrors `fold_plural`'s floor: never folds a token of three characters or
    fewer, so `'s` on a one-letter token ("x's", three characters) is left
    whole rather than stripped down to a bare, meaningless single letter.
    The floor is on the ORIGINAL token's own length, exactly as `fold_plural`
    checks the original term's length rather than the folded result's.
    """
    if len(token) <= 3:
        return token
    if token.endswith("'s"):
        return token[:-2]
    if token.endswith("'"):
        return token[:-1]
    return token


#: Function words, shared with the resolver's lexical comparison
#: (`working_set_resolve` imports this rather than keeping a second copy —
#: doing so would let the two drift, and a derived-name validity check that
#: used a different stopword list than the turn-matching one would not
#: measure "is this a name" against the same vocabulary a turn ever produces).
#: Lives here rather than in `working_set_resolve` because `derived_short_name`
#: needs it too, and this module has no dependency on the resolver.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
        "from", "how", "i", "i'm", "im", "in", "is", "it", "its", "just", "me", "much",
        "my", "no", "not", "of", "on", "or", "our", "out", "so", "still", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "to", "up", "was",
        "we", "what", "when", "where", "which", "who", "why", "will", "with", "you",
        "your", "again", "any", "does", "did", "get", "got", "had", "has", "have",
        "left", "like", "make", "many", "more", "most", "need", "now", "one", "only",
        "other", "over", "should", "some", "such", "than", "too", "use", "very",
        "want", "way", "well", "about",
        # Correction round 1, C1: "let" (a hortative auxiliary in its own
        # right, "let's" == "let us") is the one word the possessive-fold
        # BLOCKER's own required red test ("let's ship" must not reach an
        # anchor titled "Let") needs added here -- `fold_possessive("let's")`
        # folds to "let", and the C1 fix only drops a fold that LANDS IN
        # STOPWORDS, so the word itself has to be one.
        "let",
        # Correction round 1, C3 (REQUIRED, measured on a live-shaped vault):
        # this list had almost no prepositions or conjunctions, so a turn
        # sharing only ONE such function word with an anchor's title earned
        # `rare_term` -- "... before the trip" resolving an unrelated plan
        # whose title merely contains "before". CLOSED-CLASS English
        # function words only (prepositions, subordinating conjunctions,
        # modal/auxiliary verb forms, common pronouns) -- never a content
        # word, however common.
        "before", "after", "into", "onto", "during", "while", "between",
        "under", "through", "without", "within", "against", "because", "if",
        "would", "could", "been", "were", "also", "both", "each", "since",
        "until", "upon", "per", "via", "being", "am", "he", "she", "his",
        "her", "us", "shall", "may", "might", "must", "off", "down",
        "across", "toward", "towards", "among", "around",
    }
)

#: A title's leading name before a trailing parenthetical (`Bike (Trek 520,
#: 2019)`) or a dash qualifier (`Bike - Trek 520`, `Bike — Trek 520`). Matched
#: on the title's own casing; normalised by the caller.
_TRAILING_PAREN = re.compile(r"^(?P<name>.+?)\s*\([^()]*\)\s*$")
_TRAILING_DASH = re.compile(r"^(?P<name>.+?)\s+[-–—]\s+\S.*$")


def derived_short_name(title: str) -> str | None:
    """The leading name of a title carrying a trailing qualifier, or `None`.

    Structural extraction, not an inference: a title with no such qualifier
    derives nothing. Uniqueness (and rarity) across the anchor set is the
    caller's job (see `_finalize_anchor_aliases`) — this function only ever
    looks at one title, and decides only whether that title's own lead is a
    NAME at all, never whether it is anyone else's.

    Nothing is derived when the lead: is a filename (contains `.`, or starts
    with `_` — a stray extension or a private note, never a name a turn would
    say); tokenises to nothing, to more than three words, to only stopwords
    (the resolver's own list, `STOPWORDS`) or to only digits (a bare year is a
    date, not a name); contains a word longer than 48 CODE POINTS (a script
    without word separators caps a "word count" of one at three TOKENS per
    the check above, never at any length, so an unbroken CJK run of a whole
    sentence would otherwise read as a valid "three-or-fewer-word" name —
    but three ordinary compound words, German-length or longer, are still a
    name: the cap is per WORD, not on the joined whole, precisely so a
    lead of long compound words is not refused for the same reason a
    sentence is); or joins to fewer than three code points (a single letter
    or two occupies a name slot it can never fill, since a turn that short
    is dropped by the resolver's own stopword filtering before it could ever
    match). The name itself is the lead's tokens, JOINED BY SINGLE SPACES
    the way the resolver's own tokeniser would read it back — never the raw
    substring — so a multi-space or emoji-led title ("Multi   Spaces -
    qualifier", "🎯 Goal - notes") derives a name a turn can actually
    produce, instead of one that can never match and only occupies a name
    slot.
    """
    stripped = str(title).strip()
    match = _TRAILING_PAREN.match(stripped) or _TRAILING_DASH.match(stripped)
    if match is None:
        return None
    lead = match.group("name").strip()
    if not lead or "." in lead or lead.startswith("_"):
        return None
    tokens = tokens_of(lead)
    if not tokens or len(tokens) > 3:
        return None
    # Per WORD, not on the joined whole (correction round 3): three ordinary
    # compound words ("Ausrüstungsverwaltungssystem Lagerverwaltung
    # Übersicht", 28+15+9 code points, 54 joined) are still a name; only a
    # single unbroken run longer than this — the shape a sentence in a
    # script without word separators takes — is refused.
    if max(len(token) for token in tokens) > 48:
        return None
    if all(token in STOPWORDS for token in tokens):
        return None
    if all(token.isdigit() for token in tokens):
        return None
    # Per-token, not just the joined whole (review round 4, MINOR): "a" + "b"
    # joins to "a b", three characters, past the whole-name floor below, even
    # though neither token is a name fragment; "2026" + "q3" joins to "2026
    # q3" even though "q3" is one letter with a digit stapled on. A token
    # needs at least two LETTERS to be a name fragment at all. (Before task
    # 4a's Unicode-aware tokeniser, an accented word such as "élève" could
    # not stay whole under the old `[a-z0-9]` alphabet and fragmented into
    # single letters here too; `tokens_of` now reads it as one term, so this
    # check no longer needs to catch that case, only the two above.)
    if any(sum(1 for ch in token if ch.isalpha()) < 2 for token in tokens):
        return None
    name = " ".join(tokens)
    return name if len(name) >= 3 else None


class WorkingSetIndexUnavailable(RuntimeError):
    """The sidecar could not be opened or queried.

    Distinct from an empty answer on purpose: a consumer that cannot tell "I
    looked and found nothing" from "I could not look" will eventually treat the
    second as the first.
    """


@dataclass(frozen=True, slots=True)
class AnchorRow:
    """One catalogue row. Categorical and structural throughout — no scores."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    signature: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    links: tuple[tuple[str, str, str], ...]
    #: The subset of `neighbourhood` whose pages are themselves anchors here.
    #: Exposed from the index because only the index knows the anchor set, and
    #: the resolver needs it to tell a shared SENSE from a shared page: a
    #: boilerplate note two hubs both link — reached by an alias or otherwise —
    #: makes them neither complementary nor related.
    anchor_neighbourhood: frozenset[str] = frozenset()

    @property
    def neighbourhood(self) -> frozenset[str]:
        """Paths this anchor is typed-linked to, in either direction."""
        return frozenset(target for target, _relation, _direction in self.links)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An anchor as the walkers produce it, before it reaches sqlite."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    signature: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    source_signature: str


# --------------------------------------------------------------------------- #
# Structural extraction (no model, ever)
# --------------------------------------------------------------------------- #


def lede(body: str) -> str:
    """The first authored paragraph that is not a heading, capped.

    Structural extraction: the page's own first sentences, never a summary the
    server produced. Shared with the entity lane, which quotes it verbatim.
    """
    for block in body.split("\n\n"):
        text = " ".join(line.strip() for line in block.strip().splitlines() if line.strip())
        if not text or text.startswith("#") or text.startswith("---"):
            continue
        if text.startswith("- ") or text.startswith("* "):
            text = text.lstrip("-* ").strip()
        if not text:
            continue
        return text[:LEDE_MAX_CHARS]
    return ""


def _sections(body: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for match in _HEADING.finditer(body):
        seen.setdefault(match.group(1).strip(), None)
    return tuple(seen)


def _signature(title: str, body: str, *, extra: Iterable[str] = ()) -> str:
    parts = [title.strip(), lede(body), *_sections(body), *extra]
    return "\n".join(part for part in parts if part)[:SIGNATURE_MAX_CHARS]


def _categories(sections: Iterable[str], tags: Iterable[str]) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for label in (*sections, *tags):
        category = _CATEGORY_BY_LABEL.get(normalize(label))
        if category is not None:
            found.setdefault(category, None)
    return tuple(sorted(found))


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_strings(item))
        return tuple(out)
    text = str(value).strip()
    return (text,) if text else ()


def _page_ref(frontmatter: Mapping[str, Any]) -> str | None:
    from . import memory_refs

    exomem_id = str(frontmatter.get("exomem_id") or "").strip()
    return memory_refs.memory_ref(exomem_id) if exomem_id else None


# --------------------------------------------------------------------------- #
# Source walkers
# --------------------------------------------------------------------------- #


def _walk_kb(vault_root: Path):
    root = Path(vault_root) / kb_dirname()
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIR_NAMES:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.suffix.lower() == ".md":
                yield entry


def _source_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{int(stat.st_mtime_ns)}:{int(stat.st_size)}"


def _walk_page_entries(
    vault_root: Path,
) -> tuple[
    list[dict[str, Any]],
    dict[str, tuple[str, ...]],
    dict[str, list[str]],
    dict[str, list[tuple[str, str]]],
]:
    """Walk the knowledge base once: raw anchor-page entries, wikilink edges,
    a name map, and each project key's member pages.

    The name map is one name to MANY paths: `normalize()` folds case and Unicode
    form, so distinct pages can share a spelling, and collapsing them would hide
    every page but one from both edge resolution and the egress guard.

    Entries are raw dicts, NOT `_Candidate`s: a derived short name (`Bike
    (Trek 520, 2019)` -> `bike`) is only an alias while unique across, AND
    rare within, the FULL held anchor set — pages, Records collections,
    Planning items and project keys together, after the `MAX_ANCHORS` cap —
    and neither can be decided from the page walk alone. `WorkingSetIndex.
    _collect` gathers every kind, applies the cap, and only then calls
    `_finalize_anchor_aliases` over everything that made the cut.

    Project membership (`close-memory-loop` root cause 1: a project anchor
    built with no path and no links carries no material) is read from EVERY
    walked page, not only the ones that become anchors — a project's current
    material is overwhelmingly research notes, patterns, insights and
    failures, none of which are anchors on their own account. Read with
    `find_corpus.all_projects`, the SAME predicate `find_corpus.passes_filters`
    already uses to scope a project's own pages elsewhere: `page.scope` was
    considered and rejected (see `_project_candidates`'s docstring) because it
    is type-gated and does not answer "which project" for every page kind.
    Raw material (`Sources/`, `Evidence/`) is excluded for the same reason it
    is excluded from being an anchor at all: it is evidence ABOUT a project,
    never the project's own current material.
    """
    from . import recall_policy

    raw: list[dict[str, Any]] = []
    outbound: dict[str, tuple[str, ...]] = {}
    names: dict[str, list[str]] = {}
    project_members: dict[str, list[tuple[str, str]]] = {}
    kb = kb_dirname()
    for path in _walk_kb(vault_root):
        rel = path.relative_to(vault_root).as_posix()
        if recall_policy.is_structured_only_path(vault_root, rel):
            continue
        page = find_corpus.CACHE.get(path, Path(vault_root))
        if page is None:
            continue
        frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
        title = str(frontmatter.get("title") or page.title or path.stem).strip()
        # Every spelling this vault resolves for the page. Frontmatter `aliases`
        # belong here for the same reason the title does: `[[Dossier]]` is a
        # working link, so an alias IS identity, and a map without it leaves an
        # alias-spelled reference unresolvable — which the egress guard reads as
        # "names nothing" and serves. It is also the resolver's strongest evidence
        # kind (`exact_alias`), so treating it as weaker here was incoherent.
        for spelling in (
            rel.removesuffix(".md"),
            rel.removesuffix(".md").removeprefix(f"{kb}/"),
            path.stem,
            title,
            *_strings(frontmatter.get("aliases")),
        ):
            key = normalize(spelling)
            if not key:
                continue
            bucket = names.setdefault(key, [])
            if rel not in bucket:
                bucket.append(rel)
        links = tuple(
            dict.fromkeys(
                normalize(match)
                for match in _WIKILINK.findall(page.body)
                + list(_strings(frontmatter.get("relations")))
                + list(_strings(frontmatter.get("links")))
                if normalize(match)
            )
        )
        if links:
            outbound[rel] = links
        head = rel.removeprefix(f"{kb}/").split("/", 1)[0]
        if head not in _RAW_MATERIAL_FOLDERS:
            for project_key in find_corpus.all_projects(frontmatter):
                project_members.setdefault(project_key, []).append((rel, page.updated))
        kind = _page_anchor_kind(rel, frontmatter, kb=kb)
        if kind is None or path.name.casefold() in _NAVIGATION_BASENAMES:
            continue
        sections = _sections(page.body)
        tags = _strings(frontmatter.get("tags"))
        aliases = tuple(
            dict.fromkeys(normalize(alias) for alias in _strings(frontmatter.get("aliases")))
        )
        raw.append(
            {
                "anchor_id": rel,
                "path": rel,
                "ref": _page_ref(frontmatter),
                "title": title,
                "kind": kind,
                "lifecycle": normalize(frontmatter.get("status") or "active") or "active",
                "aliases": aliases,
                "sections": sections,
                "tags": tags,
                "body": page.body,
                "source_signature": _source_signature(path),
            }
        )
    return raw, outbound, names, project_members


def _title_alias_term_owners(
    entries: Iterable[tuple[str, str, Sequence[str]]],
) -> dict[str, set[str]]:
    """`(anchor_id, title, aliases)` triples -> folded term -> owning anchor ids.

    Title and alias terms ONLY, folded the same way lexical comparison folds a
    turn's terms — never the broader `terms` field (which also folds in
    section names and tags). Shared by the persisted `term_anchor_counts`
    table and the derived-short-name rarity gate below, so both measure
    rarity identically.
    """
    owners: dict[str, set[str]] = {}
    for anchor_id, title, aliases in entries:
        own_terms = frozenset(fold_plural(term) for term in tokens_of(" ".join((title, *aliases))))
        for term in own_terms:
            owners.setdefault(term, set()).add(anchor_id)
    return owners


def _derived_name_is_rare(name: str, term_owners: Mapping[str, set[str]]) -> bool:
    """Every term of a derived short name must be rare in the AUTHORED
    title/alias vocabulary, not merely textually unique.

    A dash- or parenthetical-qualified title whose leading words are a common
    TOPIC PREFIX — twenty pages titled "Orchard Strategy", "Orchard Roadmap",
    ... plus one titled "Orchard — Agentic Search" — derives "Orchard" as a
    name no other anchor's names literally include, yet the word identifies
    twenty anchors, not one. Measured the same way `rare_term` measures a shared
    turn word's rarity (`RARE_TERM_MAX_ANCHORS`), over the SAME authored-only
    counts, so a name is only ever admitted when it is genuinely rare by that
    one shared yardstick.
    """
    terms = frozenset(fold_plural(term) for term in tokens_of(name))
    if not terms:
        return False
    return all(len(term_owners.get(term, ())) <= RARE_TERM_MAX_ANCHORS for term in terms)


def _finalize_anchor_aliases(
    raw: Sequence[Mapping[str, Any]],
    other_candidates: Sequence[_Candidate] = (),
) -> tuple[list[_Candidate], dict[str, set[str]]]:
    """Build every PAGE anchor's `_Candidate`, adding a derived short name as
    an alias only while BOTH hold: no other anchor's names — title, alias, or
    own derived short name — include it, AND every one of its own terms is
    rare in the authored title/alias vocabulary (`_derived_name_is_rare`).

    `other_candidates` is every non-page anchor ALREADY HELD in the index —
    Records collections, Planning items, project keys, after the
    `MAX_ANCHORS` cap — so a derived page name cannot duplicate a collection's
    own title, and the rarity count it is measured against covers every anchor
    kind, not pages alone. It contributes to `name_owners`/`term_owners` but is
    never itself a candidate for derivation: only a page title carries a
    parenthetical or dash qualifier in the sense this function derives from.

    One pass to collect who owns which name, a second to decide: a name is
    "owned" by more than one anchor either because two anchors are genuinely
    titled or aliased alike, or because two anchors independently derive the
    same short name (a second `Bike (...)` page retires the alias for both).
    Either way the alias is withheld from all of them, never awarded to
    whichever anchor happened to be walked first.

    The rarity gate is measured against `term_owners` — computed here, from
    `raw` and `other_candidates` alone, BEFORE any derived name exists — and
    returned alongside the candidates so the caller can fold it into the
    persisted term-count table unchanged: a derived alias must never be able
    to inflate the very count that gated its own admission, or any other
    anchor's.
    """
    name_owners: dict[str, set[str]] = {}
    for entry in raw:
        for name in (entry["title"], *entry["aliases"]):
            key = normalize(name)
            if key:
                name_owners.setdefault(key, set()).add(entry["anchor_id"])
    for candidate in other_candidates:
        for name in (candidate.title, *candidate.aliases):
            key = normalize(name)
            if key:
                name_owners.setdefault(key, set()).add(candidate.anchor_id)

    term_owners = _title_alias_term_owners(
        (entry["anchor_id"], entry["title"], entry["aliases"]) for entry in raw
    )
    for term, ids in _title_alias_term_owners(
        (c.anchor_id, c.title, c.aliases) for c in other_candidates
    ).items():
        term_owners.setdefault(term, set()).update(ids)

    derived_key_by_anchor: dict[str, str] = {}
    for entry in raw:
        derived = derived_short_name(entry["title"])
        key = normalize(derived) if derived else ""
        if key:
            derived_key_by_anchor[entry["anchor_id"]] = key
            name_owners.setdefault(key, set()).add(entry["anchor_id"])

    candidates: list[_Candidate] = []
    for entry in raw:
        aliases = tuple(entry["aliases"])
        key = derived_key_by_anchor.get(entry["anchor_id"])
        if (
            key is not None
            and name_owners.get(key) == {entry["anchor_id"]}
            and _derived_name_is_rare(key, term_owners)
        ):
            aliases = (*aliases, key)
        title = str(entry["title"])
        sections = entry["sections"]
        tags = entry["tags"]
        candidates.append(
            _Candidate(
                anchor_id=entry["anchor_id"],
                path=entry["path"],
                ref=entry["ref"],
                title=title,
                kind=entry["kind"],
                lifecycle=entry["lifecycle"],
                signature=_signature(title, entry["body"]),
                aliases=aliases,
                terms=terms_of(" ".join((title, *aliases, *sections, *tags))),
                categories=_categories(sections, tags),
                source_signature=entry["source_signature"],
            )
        )
    return candidates, term_owners


def _page_anchor_kind(rel: str, frontmatter: Mapping[str, Any], *, kb: str) -> str | None:
    """Which anchor kind a walked page is, or None when it is not an anchor.

    Deliberately narrow. A note is not an anchor: it is what a lane retrieves
    once an anchor resolves, and admitting every note would turn the catalogue
    into a second copy of the corpus.
    """
    inside = rel.removeprefix(f"{kb}/")
    head = inside.split("/", 1)[0]
    if head == "Entities":
        return "entity" if normalize(frontmatter.get("type")) == "entity" else None
    if head in {"Products", "Systems"}:
        return "resource"
    if head in _RAW_MATERIAL_FOLDERS:
        # `Sources/` and `Evidence/` are immutable raw material. A captured
        # article or a preserved receipt that happens to carry `tags: [hub]` is
        # evidence ABOUT the world, not a durable anchor of the user's own
        # structure, and admitting it would let raw material name itself as the
        # subject of a turn.
        return None
    if "hub" in {normalize(tag) for tag in _strings(frontmatter.get("tags"))}:
        return "hub"
    return None


def _collection_candidates(vault_root: Path) -> tuple[list[_Candidate], list[_Candidate]]:
    """Records manifests (+ their claims) and active Planning items.

    This is the ONE place activation enumerates the vault for collection
    manifests, and it runs off the request path — a background warm or a
    watcher-driven update. What it discovers is held for `_write` to publish
    under the generation that write produces, so a request thread can read the
    manifests instead of sweeping for them again.
    """
    from . import structured_collections

    records: list[_Candidate] = []
    plans: list[_Candidate] = []
    try:
        manifests = structured_collections.discover_collections(Path(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable manifest costs its anchor, not the build
        log.debug("activation index: collection discovery failed", exc_info=True)
        _note_discovered_manifests(None)
        return records, plans
    _note_discovered_manifests(manifests)
    for manifest in manifests:
        rel = str(getattr(manifest, "path", "") or "")
        if not rel:
            continue
        absolute = Path(vault_root) / rel
        fields = tuple(getattr(getattr(manifest, "schema", None), "fields", {}) or ())
        claims = tuple(
            dict.fromkeys(
                normalize(value)
                for values in (getattr(manifest, "claims", None) or {}).values()
                for value in _strings(values)
            )
        )
        title = str(getattr(manifest, "title", "") or absolute.parent.name).strip()
        profile = str(getattr(manifest, "semantic_profile", "") or "")
        if profile == "records":
            records.append(
                _Candidate(
                    anchor_id=rel,
                    path=rel,
                    ref=None,
                    title=title,
                    kind="collection",
                    lifecycle=normalize(getattr(manifest, "lifecycle", "active") or "active"),
                    signature=_signature(title, "", extra=(*fields, *claims)),
                    aliases=(),
                    terms=terms_of(" ".join((title, *fields, *claims))),
                    categories=("fact",),
                    source_signature=_source_signature(absolute),
                )
            )
        elif profile == "planning":
            plans.extend(_planning_candidates(vault_root, manifest, rel))
    return records, plans


def _planning_candidates(vault_root: Path, manifest: Any, rel: str) -> list[_Candidate]:
    from . import planning

    try:
        result = planning.query(
            Path(vault_root),
            manifest,
            lifecycle="active",
            limit=MAX_PLAN_ITEMS,
        )
    except Exception:  # noqa: BLE001 - a planning lane that cannot read costs its anchors only
        log.debug("activation index: planning query failed for %s", rel, exc_info=True)
        return []
    rows = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return []
    signature = _source_signature(Path(vault_root) / rel)
    out: list[_Candidate] = []
    for row in rows:
        values = row.get("values") if isinstance(row, Mapping) else None
        if not isinstance(values, Mapping):
            values = row if isinstance(row, Mapping) else {}
        title = str(values.get("title") or "").strip()
        if not title:
            continue
        item_path = str(row.get("path") or "") if isinstance(row, Mapping) else ""
        kind_field = str(values.get("kind") or "").strip()
        tags = _strings(values.get("tags"))
        out.append(
            _Candidate(
                anchor_id=f"plan:{rel}#{normalize(title)}",
                path=item_path or rel,
                ref=None,
                title=title,
                kind="plan",
                lifecycle=normalize(values.get("lifecycle") or "active") or "active",
                signature=_signature(title, "", extra=(kind_field, *tags)),
                aliases=(),
                terms=terms_of(" ".join((title, kind_field, *tags))),
                categories=("action",),
                source_signature=f"{signature}:{normalize(title)}",
            )
        )
    return out


#: Bounds a project anchor's own member-page neighbourhood — the number of
#: paths recorded as the project's `links`. Deliberately its OWN constant,
#: distinct from `MAX_LINKS_PER_ANCHOR` (the generic wikilink-edge cap): a
#: project's neighbourhood is a curated "what's current in this project" set
#: the units lane reads material from, not an incidental link count, and the
#: two are tuned on different accounts.
PROJECT_ANCHOR_MEMBER_CAP = 24


def _member_recency_key(updated: str) -> tuple[int, object]:
    """Sort key: a real moment orders by `temporal.sort_key`; absent/unparsable
    sorts last. The leading `0`/`1` keeps the two shapes from ever being
    compared against each other — Python only inspects a tuple's later
    positions once the earlier ones tie, and two members with no date at all
    tie here at `(0, None)` and fall through to the caller's own tie-break.
    """
    from . import temporal

    moment = temporal.parse(updated) if updated else None
    if moment is None:
        return (0, None)
    return (1, temporal.sort_key(moment))


def _bounded_project_members(members: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The capped, deterministic member-path list for one project anchor.

    Most recently updated first, capped at `PROJECT_ANCHOR_MEMBER_CAP`. Two
    stable sorts — path ascending, then recency descending — because Python's
    `sort` is stable: the second sort keeps the first sort's order wherever
    its own key does not decide, which is exactly the deterministic
    tie-break a same-recency (or entirely undated) member set needs.
    """
    ordered = sorted(dict(members).items())
    ordered.sort(key=lambda item: _member_recency_key(item[1]), reverse=True)
    return tuple(path for path, _updated in ordered[:PROJECT_ANCHOR_MEMBER_CAP])


def _project_candidates(
    vault_root: Path, member_paths: Mapping[str, Sequence[tuple[str, str]]]
) -> tuple[list[_Candidate], dict[str, list[tuple[str, str, str]]]]:
    """Project-key anchors, and their member pages as the anchor's own links.

    `member_paths` is `project key -> (rel_path, updated)` pairs, gathered by
    `_walk_page_entries` from the single walk it already does — never a
    second enumeration. A project anchor has no page of its own (`path` stays
    `""`), so before this its `AnchorFacts.neighbourhood` was empty by
    construction: `close-memory-loop`'s design.md already named this
    "pathless project identities" that "still compete" for resolution and
    then carry no material (root cause 1). The bounded, most-recently-updated
    member subset becomes the anchor's `links`, which is exactly what
    `working_set._neighbourhood_paths` and `_units_lane` already read for
    every other anchor kind — no new lane, no new disclosure path: a member
    page still reaches a packet only through the units lane's existing
    catalogue read and the unconditional egress guard.

    Membership is read from `find_corpus.all_projects` (frontmatter `project`/
    `projects`), the SAME predicate `find_corpus.passes_filters` already
    scopes a project's pages with elsewhere. `page.scope` was the first
    candidate and was rejected: it is a per-PAGE-TYPE projection for the
    public search result shape, and for `entity`, `production-log`,
    `experiment` and `source` pages it returns the entity type/medium/domain/
    source type and never falls through to the page's own `project`/
    `projects` field at all — so it is not what ties an entity, a production
    log, an experiment or a source page to a project key, only what a search
    result shows as that page's "scope" column.
    """
    from . import project_keys

    try:
        registry = project_keys.load_project_registry(Path(vault_root))
    except Exception:  # noqa: BLE001 - a missing registry costs project anchors only
        log.debug("activation index: project registry unavailable", exc_info=True)
        return [], {}
    out: list[_Candidate] = []
    edges: dict[str, list[tuple[str, str, str]]] = {}
    for key in registry.keys:
        folder = registry.folder_for(key) or key
        category = registry.category_for(key)
        anchor_id = f"project:{key}"
        members = _bounded_project_members(member_paths.get(key, ()))
        out.append(
            _Candidate(
                anchor_id=anchor_id,
                path="",
                ref=anchor_id,
                title=str(folder).strip(),
                kind="project",
                lifecycle="active",
                signature=_signature(str(folder), "", extra=(key, category)),
                aliases=(normalize(key),),
                terms=terms_of(" ".join((str(folder), key, category))),
                categories=(),
                # The KEPT member set (post-cap) is part of what identifies
                # this generation of the anchor: it must change the source
                # signature so `update()` republishes when it does, exactly
                # as any other anchor's incremental update already reacts to
                # its own source signature changing.
                source_signature=f"{key}:{folder}:{category}:{','.join(members)}",
            )
        )
        if members:
            edges[anchor_id] = [(path, "project_member", "outbound") for path in members]
    return out, edges


def _resolve_links(
    outbound: Mapping[str, tuple[str, ...]],
    names: Mapping[str, Sequence[str]],
    anchor_paths: Mapping[str, str],
) -> dict[str, list[tuple[str, str, str]]]:
    """Turn wikilink names into edges between the pages the index knows.

    Both directions are recorded. An anchor's neighbourhood is what corroborates
    it and what bounds its lanes, and a hub that is linked TO but links nowhere
    would otherwise have no neighbourhood at all.
    """
    edges: dict[str, list[tuple[str, str, str]]] = {}

    def _add(anchor_id: str, target: str, direction: str) -> None:
        bucket = edges.setdefault(anchor_id, [])
        row = (target, "wikilink", direction)
        if row not in bucket and len(bucket) < MAX_LINKS_PER_ANCHOR:
            bucket.append(row)

    for source_rel, targets in outbound.items():
        source_anchor = anchor_paths.get(source_rel)
        for name in targets:
            for target_rel in names.get(name) or ():
                if target_rel == source_rel:
                    continue
                if source_anchor is not None:
                    _add(source_anchor, target_rel, "outbound")
                target_anchor = anchor_paths.get(target_rel)
                if target_anchor is not None:
                    _add(target_anchor, source_rel, "inbound")
    return edges


# --------------------------------------------------------------------------- #
# The sidecar
# --------------------------------------------------------------------------- #

_CACHE_LOCK = threading.Lock()
_ROW_CACHE: dict[Path, tuple[tuple[int, int, int], tuple[AnchorRow, ...]]] = {}
#: The signature-vector matrix, read once per write token: (fingerprint the
#: vectors were made with, anchor ids, read-only L2-normalised float32 matrix).
_MATRIX_CACHE: dict[Path, tuple[tuple[int, int, int], tuple[str | None, tuple[str, ...], Any]]] = {}


# --------------------------------------------------------------------------- #
# The collection-manifest registry
# --------------------------------------------------------------------------- #

#: Generations kept per vault: the current one, and one previous so a request
#: that read the generation just before an update completed is still served
#: from the registry rather than paying for a sweep of its own.
MANIFEST_REGISTRY_GENERATIONS = 2
#: Vaults kept, least-recently-published evicted first. One server serves one
#: vault; the bound exists so a process that touches many (the test suite, a
#: multi-vault tool) cannot grow this without limit.
MANIFEST_REGISTRY_VAULTS = 8

_MANIFEST_REGISTRY_LOCK = threading.Lock()

#: The identity of a vault's sidecar: its `(epoch, instance)`, generation
#: excluded. `instance` is stamped once, at random, when a sidecar's meta table
#: is created, so it differs for every sidecar ever built -- which is exactly
#: what tells a rebuilt counter apart from an older one.
_SidecarIdentity = tuple[int, int]

#: The identity a caller with no token gets. Every such caller shares it, so
#: they behave among themselves exactly as the generation-only key did, and a
#: real sidecar's entries are never served to them or theirs to it.
_ANONYMOUS_IDENTITY: _SidecarIdentity = (0, 0)

#: vault -> (that vault's sidecar identity, generation -> manifests).
#:
#: Keyed on the sidecar, not on a number, because a generation is only
#: meaningful within the sidecar that issued it. Keyed on the number alone this
#: had two measured failures, both of them the sweep coming back to the request
#: thread: a sidecar deleted and rebuilt restarts its counter at 1, so its own
#: rebuild was evicted by the dead sidecar's higher numbers and every request
#: after it missed and declined to store; and until the counter climbed back,
#: the dead sidecar's manifests were served as if they described this vault.
#: `_ROW_CACHE` next door has always keyed on the whole token for the same
#: reason.
_MANIFEST_REGISTRY: OrderedDict[
    str, tuple[_SidecarIdentity, dict[int, tuple[Any, ...]]]
] = OrderedDict()
#: What the discovery inside ONE `_write` call saw, before the write that will
#: give it a generation. A single-element box installed by `_write` before it
#: collects and read by it after, so the set an update discovered belongs to
#: that update and to nothing else.
#:
#: Keyed per vault, this was shared: a concurrent update whose discovery FAILED
#: cleared the set another update was holding, and that generation was then
#: never published at all — every request at it went back to the sweep. A
#: `ContextVar` is per-thread and per-task, and discovery and publication
#: happen in the same call on the same thread, so no update can reach another's
#: box. Each `_write` installs a fresh one, which is why nothing resets it: a
#: box left behind is replaced before it can be read again.
_PENDING_MANIFESTS: ContextVar[list[tuple[Any, ...] | None] | None] = ContextVar(
    "exomem_working_set_pending_manifests", default=None
)


def _vault_key(vault_root: Path) -> str:
    """One vault, one key, however the caller spelled it.

    Keyed on the unresolved path, a symlinked or dotted spelling of one vault
    silently got its own entry: every request through that spelling missed,
    swept, and published into a second copy nothing else read. On the read side
    this runs inside the request's resolution scope, so it costs one resolution
    per request; on the publish side it is the index update, which can afford
    one.
    """
    from . import state_paths

    return str(state_paths.resolved_vault_path(vault_root, expanduser=False))


def sidecar_identity(token: Sequence[int] | None) -> _SidecarIdentity:
    """`(epoch, instance)` from a sidecar token, or the anonymous identity."""
    if token is None:
        return _ANONYMOUS_IDENTITY
    try:
        epoch, _generation, instance = (int(part) for part in token)
    except (TypeError, ValueError):
        return _ANONYMOUS_IDENTITY
    return (epoch, instance)


def publish_collection_manifests(
    vault_root: Path,
    generation: int,
    manifests: Sequence[Any],
    *,
    token: Sequence[int] | None = None,
) -> None:
    """Publish one index generation's collection manifests for request threads.

    The value is frozen into a tuple BEFORE the lock is taken, so a reader can
    only ever see a whole generation or no generation at all — there is no
    moment at which a half-built entry is reachable. Nothing under the lock
    touches the filesystem.

    A publish under a sidecar identity this vault has not got entries for
    REPLACES them all. They describe a sidecar that no longer exists, and the
    publisher is the index update, which by construction is holding the live
    one — so the generations either side of a rebuild are not comparable and
    the old ones are not evidence about anything.
    """
    entry = tuple(manifests)
    key = _vault_key(vault_root)
    identity = sidecar_identity(token)
    with _MANIFEST_REGISTRY_LOCK:
        held = _MANIFEST_REGISTRY.get(key)
        if held is None or held[0] != identity:
            generations: dict[int, tuple[Any, ...]] = {}
            _MANIFEST_REGISTRY[key] = (identity, generations)
        else:
            generations = held[1]
        generations[int(generation)] = entry
        # By HIGHEST generation, never by publish recency, and only ever among
        # generations of ONE sidecar. Evicting the least recently published let
        # a straggler that had captured an older generation republish under its
        # stale number and push the live generations out, putting the sweep it
        # had just paid for back on the next request thread. Comparing numbers
        # across sidecars did the same thing for a different reason, which is
        # why a new identity replaces the entries rather than competing with
        # them.
        for stale in sorted(generations)[:-MANIFEST_REGISTRY_GENERATIONS]:
            generations.pop(stale, None)
        _MANIFEST_REGISTRY.move_to_end(key)
        while len(_MANIFEST_REGISTRY) > MANIFEST_REGISTRY_VAULTS:
            _MANIFEST_REGISTRY.popitem(last=False)


def published_collection_manifests(
    vault_root: Path, generation: int, *, token: Sequence[int] | None = None
) -> tuple[Any, ...] | None:
    """This generation's published manifests, or `None` when there are none.

    A generation only means anything within the sidecar that issued it, so an
    entry published under a different sidecar identity is not an answer to this
    question and is reported as absent.

    `None` and `()` are different answers and must stay so: a vault with no
    collections publishes an empty tuple, and reading that as "nothing was
    published" would charge every request a sweep that can only ever find
    nothing.
    """
    key = _vault_key(vault_root)
    identity = sidecar_identity(token)
    with _MANIFEST_REGISTRY_LOCK:
        held = _MANIFEST_REGISTRY.get(key)
        if held is None or held[0] != identity:
            return None
        return held[1].get(int(generation))


def collection_manifests(
    vault_root: Path, generation: int, *, token: Sequence[int] | None = None
) -> tuple[Any, ...]:
    """The manifests activation reads, without enumerating a single directory.

    Served from what the index update published for this generation. A miss —
    the first request in a process, a one-process-per-call CLI, an index another
    process has since moved on — computes once through the ordinary discovery
    and publishes the result, so the miss is paid once rather than per request.
    A discovery that fails raises, exactly as calling it directly would: the
    callers here already treat unavailable manifests as no evidence.
    """
    published = published_collection_manifests(vault_root, generation, token=token)
    if published is not None:
        return published
    from . import structured_collections

    manifests = tuple(structured_collections.discover_collections(Path(vault_root)))
    if _may_store(vault_root, generation, sidecar_identity(token)):
        publish_collection_manifests(vault_root, generation, manifests, token=token)
    return manifests


def _may_store(
    vault_root: Path, generation: int, identity: _SidecarIdentity
) -> bool:
    """Whether a READER may keep what it just computed.

    Two refusals, both of them "this request cannot prove its answer is the
    current one":

    * a straggler that captured an older generation of the SAME sidecar serves
      itself and leaves the registry alone — storing would be a write on behalf
      of a generation nobody is serving any more, and the entry it would occupy
      belongs to the generations that are;
    * a reader whose token names a different sidecar from the one this vault
      holds entries for stores nothing and evicts nothing. Only the index
      update, which holds the live sidecar by construction, may replace a
      vault's entries wholesale; a reader cannot tell whether its own token
      died under it or the registry's did.

    The price of that second refusal, stated so nobody has to rediscover it: if
    ANOTHER process recreates the sidecar, this process holds entries under the
    dead identity and every request here misses and declines to store, so each
    one computes for itself until this process's own index update publishes
    under the new identity. A managed runtime reaches that through the ordinary
    background build whenever the index looks stale; a runtime that never
    updates would keep computing. Letting a reader replace on an identity
    mismatch would close it — its token is the one it just read from the live
    sidecar — at the cost of one extra computation whenever a sidecar is
    recreated mid-request. That is a deliberate open choice, not an oversight.
    """
    key = _vault_key(vault_root)
    with _MANIFEST_REGISTRY_LOCK:
        held = _MANIFEST_REGISTRY.get(key)
        if held is None:
            return True
        if held[0] != identity:
            return False
        return not held[1] or int(generation) >= max(held[1])


def records_manifests(
    vault_root: Path, generation: int, *, token: Sequence[int] | None = None
) -> tuple[Any, ...]:
    """Just the Records-profile manifests, which is all activation asks for."""
    return tuple(
        manifest
        for manifest in collection_manifests(vault_root, generation, token=token)
        if str(getattr(manifest, "semantic_profile", "")) == "records"
    )


def _note_discovered_manifests(manifests: Sequence[Any] | None) -> None:
    """Hold what THIS update just discovered until its write names a generation.

    A failed discovery empties its own update's box and no other's. Outside a
    `_write` there is no box and this does nothing, which is what keeps a
    stubbed `_collection_candidates` from publishing anything.
    """
    box = _PENDING_MANIFESTS.get()
    if box is None:
        return
    box[0] = None if manifests is None else tuple(manifests)


def _publish_pending_manifests(
    vault_root: Path, generation: int, *, token: Sequence[int] | None = None
) -> None:
    """Publish the discovery this write was built from, under its generation.

    Called on EVERY completed write, including the one that found nothing
    changed. That is what makes the registry's freshness independent of whether
    the generation moved: a manifest edit that somehow left the generation
    where it was is still republished by the update that saw it.
    """
    box = _PENDING_MANIFESTS.get()
    pending = None if box is None else box[0]
    if pending is None:
        return
    publish_collection_manifests(vault_root, generation, pending, token=token)


def reset_collection_manifests_for_tests() -> None:
    """Drop every published manifest set.

    The pending box is not touched: it belongs to one in-flight `_write` on one
    thread, and there is none in flight when a test calls this.
    """
    with _MANIFEST_REGISTRY_LOCK:
        _MANIFEST_REGISTRY.clear()


class WorkingSetIndex:
    """One vault's activation sidecar. Read-mostly; every write bumps generation."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self._path: Path | None = None
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = sidecar_path(self.vault_root)
        return self._path

    def available(self) -> bool:
        """False under the kill switch. Nothing is created or opened."""
        return not disabled()

    def readable(self) -> bool:
        """Whether the sidecar can be opened, even when it has no anchors yet."""
        return self._connect() is not None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def reset(self) -> None:
        """Drop the sidecar and every cached row — the disposability guarantee."""
        self.close()
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("activation index could not be removed: %s", self.path, exc_info=True)

    def _connect(self) -> sqlite3.Connection | None:
        if disabled():
            return None
        if self._conn is not None:
            return self._conn
        try:
            sidecar_store.ensure_sidecar_parent(self.path)
            conn = sqlite3.connect(self.path, timeout=5.0)
        except (OSError, sqlite3.Error):
            log.warning("activation index unavailable at %s", self.path, exc_info=True)
            return None
        sidecar_store.apply_sidecar_pragmas(conn)
        try:
            self._ensure_schema(conn)
        except sqlite3.Error:
            log.warning("activation index schema could not be prepared", exc_info=True)
            conn.close()
            return None
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anchors (
                anchor_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                ref TEXT,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                signature TEXT NOT NULL,
                source_signature TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_aliases "
            "(anchor_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (anchor_id, alias))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_categories "
            "(anchor_id TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY (anchor_id, category))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_links ("
            "anchor_id TEXT NOT NULL, other_path TEXT NOT NULL, "
            "relation_type TEXT NOT NULL, direction TEXT NOT NULL, "
            "PRIMARY KEY (anchor_id, other_path, relation_type, direction))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_vectors "
            "(anchor_id TEXT PRIMARY KEY, signature_digest TEXT NOT NULL DEFAULT '', vector BLOB NOT NULL)"
        )
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS anchor_terms "
                "USING fts5(terms, anchor_id UNINDEXED, tokenize='unicode61')"
            )
        except sqlite3.Error:
            # FTS5 absent: `lexical_overlap` falls back to the stored term rows.
            log.info("activation index: FTS5 unavailable; lexical overlap uses stored terms")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_term_rows "
            "(anchor_id TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY (anchor_id, term))"
        )
        # v5: how many anchors' OWN title/alias names a normalised, folded term
        # — never the broader `terms` (which also folds in sections and tags).
        # This is the structural count `rare_term` measures rarity against; it
        # is never a relevance score and never leaves the server.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS term_anchor_counts "
            "(term TEXT PRIMARY KEY, anchor_count INTEGER NOT NULL)"
        )
        # `meta` is integer-valued by the shared sidecar contract, and the
        # freshness key the index was built at is text. A second, text-valued
        # table keeps the shared token table exactly as every other sidecar
        # writes it rather than widening its type for one consumer.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS anchor_term_rows_term ON anchor_term_rows(term)")
        # Every KB page's wikilink-resolvable names -> its vault-relative path.
        # `_walk_page_entries` already computes this map to resolve anchor links; it
        # is persisted rather than discarded so the egress guard can resolve a
        # stem found in authored prose WITHOUT a corpus walk and without needing a
        # warm semantic snapshot. It covers the vault's page set, not only anchors.
        # Keyed on the PAIR, not the name: `normalize()` folds case and Unicode
        # form, so `Widget.md` and `widget.md` share a key. A name resolves to
        # EVERY path that bears it, and the guard decides all of them — otherwise
        # the page that lost the row is never decided and a wikilink to the shared
        # stem is served against it.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS page_names "
            "(name TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (name, path))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS page_names_name ON page_names(name)")
        sidecar_store.ensure_meta_table(conn, "anchors", self.path.name)
        stored = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if stored is None:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        elif int(stored[0]) != SCHEMA_VERSION:
            # A table whose columns changed is recreated, not just emptied. The
            # digest defaults to '' so the previous release's two-column insert
            # still succeeds after a rollback; an empty digest never matches.
            conn.execute("DROP TABLE IF EXISTS anchor_vectors")
            conn.execute(
                "CREATE TABLE anchor_vectors "
                "(anchor_id TEXT PRIMARY KEY, signature_digest TEXT NOT NULL DEFAULT '', vector BLOB NOT NULL)"
            )
            self._wipe(conn)

    def _wipe(self, conn: sqlite3.Connection) -> None:
        """Scoped wipe: the derived rows go, the generation token keeps counting.

        Resetting the token instead would let a consumer that cached rows from
        the old schema believe its cache is current.
        """
        for table in (
            "anchors",
            "anchor_aliases",
            "anchor_categories",
            "anchor_links",
            "anchor_vectors",
            "anchor_term_rows",
            "page_names",
            "term_anchor_counts",
            "index_meta",
        ):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM anchor_terms")
        except sqlite3.Error:
            pass
        sidecar_store.bump_meta(conn, "generation")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)
            _MATRIX_CACHE.pop(self.path, None)

    # -- reads -------------------------------------------------------------- #

    def generation(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        return sidecar_store.read_meta_token(conn)[1]

    def token(self) -> tuple[int, int, int]:
        conn = self._connect()
        if conn is None:
            return (0, 0, 0)
        return sidecar_store.read_meta_token(conn)

    def freshness_stamp(self) -> str:
        """The recall freshness key this catalogue was last built at, or `""`.

        The staleness signal. An index whose stamp is behind the request's key was
        built from an older vault state, and the operation must refresh it or say
        so — never serve a packet whose generation block implies otherwise.
        """
        conn = self._connect()
        if conn is None:
            return ""
        try:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'freshness_key'"
            ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row else ""

    def resolve_names(self, names: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """Map wikilink names to EVERY vault path that bears them.

        The same resolution `_resolve_links` performs at build time, exposed for
        the egress guard: a bare stem out of authored prose is not a
        vault-relative path, and handing one to a release decision returns "no
        decision", which is indistinguishable from "withheld". An indexed lookup
        answers it with no filesystem work.

        Two outcomes that must not be conflated:

        * a name absent from the map resolves to nothing and is simply not in the
          result — it names no page, so there is nothing to decide; and
        * a query that cannot run RAISES. A resolver that answered "no names
          resolved" when it merely failed would remove the filter precisely when
          removing it is least safe, which is the fail-open class the guard exists
          to prevent. The caller decides what a failure means; this method will
          not decide it by returning a plausible-looking empty answer.
        """
        wanted = [normalize(name) for name in names]
        wanted = [name for name in dict.fromkeys(wanted) if name]
        if not wanted:
            return {}
        conn = self._connect()
        if conn is None:
            raise WorkingSetIndexUnavailable(
                "the activation index could not be opened for name resolution"
            )
        out: dict[str, list[str]] = {}
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT name, path FROM page_names WHERE name IN ({placeholders}) "
                "ORDER BY name, path",
                chunk,
            ).fetchall()
            for name, path in rows:
                out.setdefault(str(name), []).append(str(path))
        return {name: tuple(paths) for name, paths in out.items()}

    def anchors(self) -> tuple[AnchorRow, ...]:
        """Every anchor row, copy-on-write cached on the sidecar's write token."""
        conn = self._connect()
        if conn is None:
            return ()
        token = sidecar_store.read_meta_token(conn)
        with _CACHE_LOCK:
            cached = _ROW_CACHE.get(self.path)
            if cached is not None and cached[0] == token:
                return cached[1]
        rows = self._read_rows(conn)
        with _CACHE_LOCK:
            _ROW_CACHE[self.path] = (token, rows)
        return rows

    def _read_rows(self, conn: sqlite3.Connection) -> tuple[AnchorRow, ...]:
        aliases: dict[str, list[str]] = {}
        for anchor_id, alias in conn.execute(
            "SELECT anchor_id, alias FROM anchor_aliases ORDER BY anchor_id, alias"
        ):
            aliases.setdefault(anchor_id, []).append(alias)
        categories: dict[str, list[str]] = {}
        for anchor_id, category in conn.execute(
            "SELECT anchor_id, category FROM anchor_categories ORDER BY anchor_id, category"
        ):
            categories.setdefault(anchor_id, []).append(category)
        terms: dict[str, list[str]] = {}
        for anchor_id, term in conn.execute(
            "SELECT anchor_id, term FROM anchor_term_rows ORDER BY anchor_id, term"
        ):
            terms.setdefault(anchor_id, []).append(term)
        links: dict[str, list[tuple[str, str, str]]] = {}
        for anchor_id, other, relation, direction in conn.execute(
            "SELECT anchor_id, other_path, relation_type, direction FROM anchor_links "
            "ORDER BY anchor_id, other_path, relation_type, direction"
        ):
            links.setdefault(anchor_id, []).append((other, relation, direction))
        anchor_paths = frozenset(
            str(row[0]) for row in conn.execute("SELECT path FROM anchors")
        )
        out: list[AnchorRow] = []
        for anchor_id, path, ref, title, kind, lifecycle, signature in conn.execute(
            "SELECT anchor_id, path, ref, title, kind, lifecycle, signature FROM anchors "
            "ORDER BY anchor_id"
        ):
            out.append(
                AnchorRow(
                    anchor_id=anchor_id,
                    path=path,
                    ref=ref,
                    title=title,
                    kind=kind,
                    lifecycle=lifecycle,
                    signature=signature,
                    aliases=tuple(aliases.get(anchor_id, ())),
                    terms=tuple(terms.get(anchor_id, ())),
                    categories=tuple(categories.get(anchor_id, ())),
                    links=tuple(links.get(anchor_id, ())),
                    anchor_neighbourhood=frozenset(
                        other for other, _relation, _direction in links.get(anchor_id, ())
                    )
                    & anchor_paths,
                )
            )
        return tuple(out)

    def term_anchor_counts(self) -> dict[str, int]:
        """How many anchors' own title/alias names each folded term, by term.

        Read fresh from the sidecar rather than cached: it is small (bounded by
        the anchor set's distinct title/alias vocabulary) and read once per
        `activate_context` call, unlike `anchors()`, which every candidate-
        generation pass over every row would otherwise re-query per row.
        """
        conn = self._connect()
        if conn is None:
            return {}
        try:
            rows = conn.execute("SELECT term, anchor_count FROM term_anchor_counts").fetchall()
        except sqlite3.Error:
            return {}
        return {str(term): int(count) for term, count in rows}

    def vector_matrix(self, fingerprint: str | None) -> tuple[tuple[str, ...], Any]:
        """Anchor ids and their L2-normalised signature matrix, for `fingerprint`.

        Read once per write token and shared read-only. Empty, `((), None)`,
        when no vectors exist or another encoder made them: two vector spaces
        never meet, and a background pass re-embeds under the new one.
        """
        if fingerprint is None:
            return (), None
        stored_fingerprint, ids, matrix = self._matrix_entry()
        if matrix is None or stored_fingerprint != fingerprint:
            return (), None
        return ids, matrix

    def vector_fingerprint(self) -> str | None:
        """The fingerprint the stored signature vectors were made under, or None
        when there are none. Read from the same cached entry as the matrix."""
        stored_fingerprint, _ids, matrix = self._matrix_entry()
        return stored_fingerprint if matrix is not None else None

    def _matrix_entry(self) -> tuple[str | None, tuple[str, ...], Any]:
        conn = self._connect()
        if conn is None:
            return None, (), None
        token = sidecar_store.read_meta_token(conn)
        with _CACHE_LOCK:
            cached = _MATRIX_CACHE.get(self.path)
        if cached is not None and cached[0] == token:
            return cached[1]
        entry = self._read_matrix(conn)
        with _CACHE_LOCK:
            _MATRIX_CACHE[self.path] = (token, entry)
        return entry

    def _read_matrix(self, conn: sqlite3.Connection) -> tuple[str | None, tuple[str, ...], Any]:
        # One read snapshot for the fingerprint and the rows: a writer that
        # re-embeds under another encoder between two autocommit reads would
        # otherwise hand this reader its vectors under the old fingerprint.
        own = not conn.in_transaction
        if own:
            conn.execute("BEGIN")
        try:
            stored = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'activation_encoder_fingerprint'"
            ).fetchone()
            rows = conn.execute(
                "SELECT anchor_id, vector FROM anchor_vectors ORDER BY anchor_id"
            ).fetchall()
        finally:
            if own:
                conn.rollback()
        if stored is None or not rows:
            return (str(stored[0]) if stored else None), (), None
        import numpy as np

        vectors = [(str(anchor_id), np.frombuffer(blob, dtype=np.float32)) for anchor_id, blob in rows]
        width = vectors[0][1].shape[0]
        kept = [(anchor_id, vector) for anchor_id, vector in vectors if vector.shape[0] == width]
        matrix = np.vstack([vector for _anchor_id, vector in kept]).astype(np.float32)
        matrix = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
        matrix.setflags(write=False)
        return str(stored[0]), tuple(anchor_id for anchor_id, _vector in kept), matrix

    def vectors(self) -> dict[str, Any]:
        """Signature embeddings, or `{}` when the backend produced none."""
        conn = self._connect()
        if conn is None:
            return {}
        rows = conn.execute("SELECT anchor_id, vector FROM anchor_vectors").fetchall()
        if not rows:
            return {}
        import numpy as np

        return {
            anchor_id: np.frombuffer(blob, dtype=np.float32)
            for anchor_id, blob in rows
        }

    # -- writes ------------------------------------------------------------- #

    def rebuild(self, *, freshness_stamp: str | None = None, load_encoder: bool = False) -> dict[str, Any]:
        """Derive every anchor from the vault and replace the catalogue.

        `load_encoder` is for a background pass only: it may load a cold
        activation encoder to embed signatures. Without it, signatures are
        embedded only by an encoder that is already resident, a bounded number
        per pass (`INLINE_VECTOR_ENCODE_LIMIT`), so a request thread never loads
        a model and never pays for a whole catalogue.
        """
        return self._write(full=True, freshness_stamp=freshness_stamp, load_encoder=load_encoder)

    def update(self, *, freshness_stamp: str | None = None, load_encoder: bool = False) -> dict[str, Any]:
        """Bring the catalogue to the current vault state, bumping only on change.

        Also brings the signature vectors up to date, re-embedding only the
        anchors whose signature changed (see `rebuild` for `load_encoder`).
        """
        return self._write(full=False, freshness_stamp=freshness_stamp, load_encoder=load_encoder)

    def _write(
        self, *, full: bool, freshness_stamp: str | None = None, load_encoder: bool = False
    ) -> dict[str, Any]:
        if disabled():
            return {"anchors": 0, "generation": 0, "disabled": True}
        conn = self._connect()
        if conn is None:
            return {"anchors": 0, "generation": 0, "unavailable": True}
        # This update's own box for what its discovery finds, installed before
        # collecting and read at every publish point below. A fresh one per
        # write is what keeps two concurrent updates of one vault out of each
        # other's way, so it is never reset: the next write replaces it.
        _PENDING_MANIFESTS.set([None])
        candidates, edges, page_names, term_counts = self._collect()
        existing = {
            anchor_id: signature
            for anchor_id, signature in conn.execute(
                "SELECT anchor_id, source_signature FROM anchors"
            )
        }
        wanted = {candidate.anchor_id: candidate for candidate in candidates}
        changed = full or set(existing) != set(wanted)
        if not changed:
            changed = any(
                existing[anchor_id] != candidate.source_signature
                for anchor_id, candidate in wanted.items()
            )
        if not changed:
            changed = self._links_differ(conn, edges)
        if not changed:
            stored_names = conn.execute("SELECT COUNT(*) FROM page_names").fetchone()
            wanted_names = sum(len(paths) for paths in page_names.values())
            changed = int(stored_names[0] if stored_names else 0) != wanted_names
        plan = self._plan_vectors(conn, candidates, load_encoder=load_encoder)
        if not changed and plan is not None and plan.changed:
            # The catalogue did not move but its vectors did: an encoder became
            # resident, changed, or caught up on a bounded inline pass.
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._apply_vectors(conn, plan)
                if freshness_stamp is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('freshness_key', ?)",
                        (freshness_stamp,),
                    )
                generation = sidecar_store.bump_meta(conn, "generation")
                vector_token = sidecar_store.read_meta_token(conn)
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
                log.warning("activation index vector write failed", exc_info=True)
                return {"anchors": len(existing), "generation": 0, "unavailable": True}
            _publish_pending_manifests(self.vault_root, generation, token=vector_token)
            return {"anchors": len(existing), "generation": generation, "vectors": len(plan.rows)}
        if not changed:
            # The vault did not move, so the rows and the generation must not
            # either; only the stamp advances, so the next request stops asking.
            if freshness_stamp is not None:
                self._stamp(conn, freshness_stamp)
            unchanged_token = sidecar_store.read_meta_token(conn)
            # Published even here: this update DID re-read the manifests, so the
            # registry entry for this generation is current whether or not the
            # generation moved.
            _publish_pending_manifests(
                self.vault_root, unchanged_token[1], token=unchanged_token
            )
            return {
                "anchors": len(existing),
                "generation": unchanged_token[1],
                "unchanged": True,
            }
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "anchors",
                "anchor_aliases",
                "anchor_categories",
                "anchor_links",
                "anchor_term_rows",
                "page_names",
                "term_anchor_counts",
            ):
                conn.execute(f"DELETE FROM {table}")
            if plan is None:
                conn.execute("DELETE FROM anchor_vectors")
                conn.execute("DELETE FROM index_meta WHERE key = 'activation_encoder_fingerprint'")
            else:
                self._apply_vectors(conn, plan)
            self._delete_fts(conn)
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO anchors (anchor_id, path, ref, title, kind, lifecycle, "
                    "signature, source_signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate.anchor_id,
                        candidate.path,
                        candidate.ref,
                        candidate.title,
                        candidate.kind,
                        candidate.lifecycle,
                        candidate.signature,
                        candidate.source_signature,
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_aliases (anchor_id, alias) VALUES (?, ?)",
                    [(candidate.anchor_id, alias) for alias in candidate.aliases],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_categories (anchor_id, category) VALUES (?, ?)",
                    [(candidate.anchor_id, category) for category in candidate.categories],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_term_rows (anchor_id, term) VALUES (?, ?)",
                    [(candidate.anchor_id, term) for term in candidate.terms],
                )
                self._insert_fts(conn, candidate)
            conn.executemany(
                "INSERT OR REPLACE INTO page_names (name, path) VALUES (?, ?)",
                sorted(
                    (name, path)
                    for name, paths in page_names.items()
                    for path in paths
                ),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO term_anchor_counts (term, anchor_count) VALUES (?, ?)",
                sorted(term_counts.items()),
            )
            for anchor_id, rows in edges.items():
                if anchor_id not in wanted:
                    continue
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_links "
                    "(anchor_id, other_path, relation_type, direction) VALUES (?, ?, ?, ?)",
                    [(anchor_id, *row) for row in rows],
                )
            if freshness_stamp is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES "
                    "('freshness_key', ?)",
                    (freshness_stamp,),
                )
            generation = sidecar_store.bump_meta(conn, "generation")
            # The whole token, inside the same transaction that bumped it: the
            # registry is keyed on the sidecar that issued a generation, and
            # `bump_meta` returns only the number.
            written_token = sidecar_store.read_meta_token(conn)
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            log.warning("activation index write failed", exc_info=True)
            return {"anchors": 0, "generation": 0, "unavailable": True}
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)
        _publish_pending_manifests(self.vault_root, generation, token=written_token)
        return {"anchors": len(candidates), "generation": generation}

    def _plan_vectors(
        self, conn: sqlite3.Connection, candidates: Sequence[_Candidate], *, load_encoder: bool
    ) -> _VectorPlan | None:
        """The signature vectors this write should leave, encoding only what changed.

        A stored vector is reused when its anchor's signature digest is
        unchanged and the encoder in use made it. Without a resident encoder
        (and no leave to load one) nothing is encoded: unchanged anchors keep
        their vectors and changed ones lose theirs. A resident encoder other
        than the one the vectors were made with is treated the same way on an
        inline pass: it never starts its own vector space there, because a
        bounded inline pass would leave a partial population for the band to
        calibrate on. Only a background pass re-embeds everything under it.
        None when embeddings are off, which leaves no vectors at all.
        """
        if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
            return None
        stored = {
            str(anchor_id): (str(digest), bytes(blob))
            for anchor_id, digest, blob in conn.execute(
                "SELECT anchor_id, signature_digest, vector FROM anchor_vectors"
            )
        }
        row = conn.execute(
            "SELECT value FROM index_meta WHERE key = 'activation_encoder_fingerprint'"
        ).fetchone()
        stored_fingerprint = str(row[0]) if row else None
        wanted = {
            candidate.anchor_id: _signature_digest(candidate.signature)
            for candidate in candidates
            if candidate.signature
        }
        try:
            from . import embeddings

            fingerprint = embeddings.activation_fingerprint()
        except Exception:  # noqa: BLE001 - the vector lane is optional by contract
            log.info("activation index: encoder state unavailable", exc_info=True)
            return _VectorPlan(rows={}, fingerprint=None, changed=bool(stored))

        def reusable(space: str | None) -> dict[str, tuple[str, bytes]]:
            if space is None or space != stored_fingerprint:
                return {}
            return {
                anchor_id: stored[anchor_id]
                for anchor_id, digest in wanted.items()
                if anchor_id in stored and stored[anchor_id][0] == digest
            }

        if not load_encoder and fingerprint is not None and stored_fingerprint not in (None, fingerprint):
            keep = reusable(stored_fingerprint)
            space = stored_fingerprint if keep else None
            return _VectorPlan(
                rows=keep, fingerprint=space, changed=keep != stored or space != stored_fingerprint
            )
        keep = reusable(fingerprint or stored_fingerprint)
        missing = [c for c in candidates if c.signature and c.anchor_id not in keep]
        new: dict[str, tuple[str, bytes]] = {}
        if missing:
            try:
                if fingerprint is None and load_encoder:
                    embeddings.get_activation_model()
                    fingerprint = embeddings.activation_fingerprint()
                    keep = reusable(fingerprint)
                    missing = [c for c in candidates if c.signature and c.anchor_id not in keep]
                if missing and fingerprint is not None:
                    batch = missing if load_encoder else missing[:INLINE_VECTOR_ENCODE_LIMIT]
                    import numpy as np

                    texts = [c.signature for c in batch]
                    # A request thread's pass uses a resident model or none: the
                    # reaper may unload it between the residency check and here.
                    matrix = (
                        embeddings.embed_activation_passages(texts)
                        if load_encoder
                        else embeddings.embed_activation_passages_if_loaded(texts)
                    )
                    if matrix is not None and embeddings.activation_fingerprint() == fingerprint:
                        new = {
                            c.anchor_id: (wanted[c.anchor_id], np.asarray(vector, dtype=np.float32).tobytes())
                            for c, vector in zip(batch, matrix, strict=True)
                        }
            except Exception:  # noqa: BLE001 - the vector lane is optional by contract
                log.info("activation index: signature embeddings unavailable", exc_info=True)
        rows = {**keep, **new}
        space = fingerprint if fingerprint is not None else stored_fingerprint
        if not rows:
            space = None
        changed = rows != stored or space != stored_fingerprint
        return _VectorPlan(rows=rows, fingerprint=space, changed=changed)

    def _apply_vectors(self, conn: sqlite3.Connection, plan: _VectorPlan) -> None:
        conn.execute("DELETE FROM anchor_vectors")
        conn.executemany(
            "INSERT INTO anchor_vectors (anchor_id, signature_digest, vector) VALUES (?, ?, ?)",
            sorted((anchor_id, digest, blob) for anchor_id, (digest, blob) in plan.rows.items()),
        )
        if plan.fingerprint is None:
            conn.execute("DELETE FROM index_meta WHERE key = 'activation_encoder_fingerprint'")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('activation_encoder_fingerprint', ?)",
                (plan.fingerprint,),
            )

    def _stamp(self, conn: sqlite3.Connection, freshness_stamp: str) -> None:
        """Record the freshness key without touching the write generation."""
        try:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES "
                "('freshness_key', ?)",
                (freshness_stamp,),
            )
            conn.commit()
        except sqlite3.Error:
            log.debug("activation index freshness stamp could not be written", exc_info=True)

    def _links_differ(
        self, conn: sqlite3.Connection, edges: Mapping[str, list[tuple[str, str, str]]]
    ) -> bool:
        stored: set[tuple[str, str, str, str]] = {
            (anchor_id, other, relation, direction)
            for anchor_id, other, relation, direction in conn.execute(
                "SELECT anchor_id, other_path, relation_type, direction FROM anchor_links"
            )
        }
        wanted = {
            (anchor_id, *row) for anchor_id, rows in edges.items() for row in rows
        }
        return stored != wanted

    def _delete_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("DELETE FROM anchor_terms")
        except sqlite3.Error:
            pass

    def _insert_fts(self, conn: sqlite3.Connection, candidate: _Candidate) -> None:
        try:
            conn.execute(
                "INSERT INTO anchor_terms (terms, anchor_id) VALUES (?, ?)",
                (" ".join(candidate.terms), candidate.anchor_id),
            )
        except sqlite3.Error:
            pass

    def _collect(
        self,
    ) -> tuple[
        list[_Candidate],
        dict[str, list[tuple[str, str, str]]],
        dict[str, list[str]],
        dict[str, int],
    ]:
        """Gather every anchor kind, cap to `MAX_ANCHORS`, THEN decide derived
        page aliases and measure term rarity — both over the full, capped
        catalogue, never pages alone (review round 3, MAJOR 6/7): a derived
        page name must not duplicate a collection's own title, and the count
        `rare_term` and the derived-name rarity gate measure against must
        cover exactly the anchors this index actually holds, of every kind.
        """
        raw_pages, outbound, names, project_members = _walk_page_entries(self.vault_root)
        records, plans = _collection_candidates(self.vault_root)
        projects, project_edges = _project_candidates(self.vault_root, project_members)

        # Cap in the SAME order `anchors()` has always reported (pages first),
        # over anchor identities only — page entries are not `_Candidate`s yet.
        # Deduped with `dict.fromkeys` BEFORE the slice (review round 4,
        # MINOR): every anchor id here is built from a distinct page path,
        # collection path, `plan:<rel>#<title>` key or project key, so a
        # collision is not expected — but the slice below assumes it, and an
        # undeduped list would let a duplicate id consume two cap "positions"
        # while contributing only one distinct anchor, silently starving a
        # genuinely different anchor that had room under the cap.
        ordered_ids = list(
            dict.fromkeys(
                [entry["anchor_id"] for entry in raw_pages]
                + [c.anchor_id for c in records]
                + [c.anchor_id for c in plans]
                + [c.anchor_id for c in projects]
            )
        )
        kept_ids = frozenset(ordered_ids[:MAX_ANCHORS])
        raw_pages = [entry for entry in raw_pages if entry["anchor_id"] in kept_ids]
        other_candidates = [
            c for c in (*records, *plans, *projects) if c.anchor_id in kept_ids
        ]

        pages, term_owners = _finalize_anchor_aliases(raw_pages, other_candidates)
        candidates = [*pages, *other_candidates]
        anchor_paths = {
            candidate.path: candidate.anchor_id
            for candidate in candidates
            if candidate.path
        }
        edges = _resolve_links(outbound, names, anchor_paths)
        # A project anchor's member links, merged the same way a page's own
        # wikilink edges are: only for an anchor that survived the cap (the
        # same guard `_resolve_links` gets for free from `anchor_paths`
        # already being built from `kept_ids`).
        for anchor_id, rows in project_edges.items():
            if anchor_id not in kept_ids:
                continue
            edges.setdefault(anchor_id, []).extend(rows)
        candidates.sort(key=lambda candidate: candidate.anchor_id)
        term_counts = {term: len(ids) for term, ids in term_owners.items()}
        return candidates, edges, names, term_counts


@dataclass(frozen=True)
class _VectorPlan:
    """The signature vectors one index write leaves: anchor id -> (signature
    digest, float32 bytes), the encoder they belong to, and whether that
    differs from what is stored."""

    rows: dict[str, tuple[str, bytes]]
    fingerprint: str | None
    changed: bool


def _signature_digest(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8", "surrogatepass")).hexdigest()[:32]
