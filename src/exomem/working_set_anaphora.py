"""When a turn may take its subject from the conversation (thread-aware
compilation, ruling C1 on #1463, round 3).

The conversation carry runs for a turn that (a) resolves no anchor of its own,
which the compiler decides, and here:

* (c) POINTS BACK: a pronoun or possessive, a demonstrative, a shipped
  follow-up marker, an ordinal followed by "one" or "option", a governed bare
  pointer ("that one", "the other one", "which one"), an elliptical "what
  about ..." opener, or the vault's referential cue;
* (b) BRINGS NO NEW CONTENT: every content word of the turn already occurs in
  the earlier turns. A content word is any word that is not a function word,
  not a time word, not in the closed generic task vocabulary below and not in
  the vault's referential vocabulary. One new content word is a topic switch:
  "does it snow much in oslo", "how do i descale a kettle", "it's been a long
  day" all bring words of their own, so none is carried.

A pronoun alone says little; the content gate carries the weight. These tables
are parts of speech and a generic task vocabulary, never a vault's referential
cues, which stay vault data (`tests/test_activation_conventions_referential.py`
pins that).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .working_set_index import STOPWORDS, fold_plural

PERSONAL_ANAPHORS: frozenset[str] = frozenset(
    {"he", "she", "him", "her", "hers", "his", "it", "its", "they", "them", "their", "theirs"}
)
DEMONSTRATIVES: frozenset[str] = frozenset({"this", "that", "those", "these"})
#: An ordinal followed by one of these points at an item of an earlier list.
ORDINALS: frozenset[str] = frozenset(
    {"first", "second", "third", "fourth", "fifth", "sixth", "last", "next", "other"}
)
ORDINAL_HEADS: frozenset[str] = frozenset({"one", "ones", "option", "options"})
#: Pointing words that are only anaphors when something governs them: "that
#: one", "the other one", "which one", but not "for one day" or "no one".
BARE_POINTERS: frozenset[str] = frozenset({"one", "ones", "other"})
#: What may govern a bare pointer: a determiner, a demonstrative, an ordinal.
DETERMINERS: frozenset[str] = frozenset({"the", "a", "an", "which"})
#: Ellipsis: "what about X?" reuses the previous question.
_ELLIPTICAL_OPENERS: tuple[tuple[str, ...], ...] = (
    ("what", "about"),
    ("how", "about"),
    ("and", "what", "about"),
    ("and", "how", "about"),
)

#: Words that carry no topic: pronouns, determiners, auxiliaries, wh-words,
#: prepositions and conjunctions beyond the shared stopwords, discourse
#: particles, and light verbs ("get", "take", "say") in any inflection.
_FUNCTION_WORDS: frozenset[str] = STOPWORDS | PERSONAL_ANAPHORS | DEMONSTRATIVES | frozenset(
    """
    i me my mine myself you your yours yourself yourselves himself herself itself
    we us our ours ourselves themselves one ones
    am is are was were be been being do does did doing have has had having will
    would shall should can could may might must not no nor
    what who whom whose which where when why how whatever whoever
    a an the some any all every each both either neither another other such much
    many more most few less least lot lots several enough
    anything something nothing everything anyone someone everyone nobody anybody
    somebody everybody anywhere somewhere everywhere person people
    ok okay yes yeah yep nope so well please just still now then also even ever
    yet already again else anyway really actually maybe perhaps though although
    there here too very quite given back end
    about above across after against along among around as at before behind below
    beside between beyond by down during except for from in inside into like near
    of off on onto out outside over past since than through till to toward under
    until up upon via with within without and or but if because while whether unless
    get got gotten go went gone make made take took taken give gave given put let
    see saw seen look know knew known think thought want need come came try keep
    kept seem sound feel felt happen say said tell told ask mean meant use find found
    """.split()
)
#: Time words: a turn naming a time names no subject ("this week", "last year").
_TIME_WORDS: frozenset[str] = frozenset(
    """
    today tonight tomorrow yesterday morning afternoon evening night day week
    weekend fortnight month year quarter season spring summer autumn fall winter
    time hour minute moment date later earlier soon ago recently lately early late
    monday tuesday wednesday thursday friday saturday sunday january february march
    april may june july august september october november december
    """.split()
)
#: The closed generic task vocabulary: words for asking about a work item's
#: state, owner, decision, timing, cost, risk or outcome, which any follow-up
#: may use without naming anything new. Kept short on purpose; everything else
#: is content.
TASK_VOCABULARY: frozenset[str] = frozenset(
    """
    update latest news progress track block blocked blocker stuck due deadline
    done finish ready open close closed start ship merge live delay slip late
    owns owner owned handle lead involved responsible assign team chase follow
    decide decision agree approve approval sign signoff pick chose chosen plan idea
    reply respond response hear heard remind mention explain feedback
    reason outcome result risk issue problem impact affect change numbers figures
    cost price total compare comparison better worse best worst cheaper right wrong
    true correct different important urgent priority version draft review detail
    expand fix fail broken round phase stage cycle sprint milestone step
    budget current final prefer check page move worth send own push settle reject
    safe summarise summarize drop revisit manager expensive matter
    """.split()
)
#: A time word is no topic only where one of these places it relative to now
#: or to the thread ("last year", "next month", "the autumn", "this week"). An
#: ungoverned one is content: "what day is it today", "autumn came early".
_TIME_GOVERNORS: frozenset[str] = frozenset(
    """next last this these previous coming following the every a an on by in
    within until before after since""".split()
)
#: Time adverbs: "revisit this tomorrow" keeps its demonstrative.
_TIME_ADVERBS: frozenset[str] = frozenset(
    "today tonight tomorrow yesterday later soon ago recently lately early late".split()
)
#: The pointing words themselves are never content.
_POINTERS: frozenset[str] = (
    ORDINALS | ORDINAL_HEADS | frozenset({"former", "latter", "previous", "above", "earlier", "same"})
)
#: Everything that is never content, bar time words and the vault's vocabulary.
_KNOWN: frozenset[str] = _FUNCTION_WORDS | TASK_VOCABULARY | _POINTERS
_CLITIC_VERBS: dict[str, str] = {"s": "is", "re": "are", "ll": "will", "d": "would", "ve": "have", "m": "am"}
#: "n't" stems that are not the verb minus its final "n".
_NEGATED: dict[str, str] = {"won": "will", "can": "can", "shan": "shall", "ain": "is"}
#: Everything that is never content, bar the vault's own vocabulary.


def _words(tokens: Iterable[str]) -> list[str]:
    """Tokens with contractions split ("it's" -> "it", "is"; "isn't" -> "isn",
    "not"), a possessive folded to its noun, and hyphenated words split."""
    words: list[str] = []
    for token in tokens:
        head, apostrophe, tail = token.partition("'")
        if apostrophe and head:
            if tail == "t":
                words.extend((_NEGATED.get(head, head[:-1] if head.endswith("n") else head), "not"))
                continue
            words.append(head)
            if tail in _CLITIC_VERBS and head in _FUNCTION_WORDS:
                words.append(_CLITIC_VERBS[tail])
            continue
        words.extend(part for part in token.split("-") if part)
    return words


def forms(word: str) -> frozenset[str]:
    """`word` and its regular inflectional stems: plural, -s, -es, -ed, -ing,
    comparative -er, with a doubled final consonant undone ("stopped" ->
    "stop")."""
    out = {word, fold_plural(word)}
    for suffix, replacements in (
        ("ies", ("y",)),
        ("ied", ("y",)),
        ("es", ("",)),
        ("s", ("",)),
        ("ed", ("", "e")),
        ("ing", ("", "e")),
        ("er", ("", "e")),
    ):
        if word.endswith(suffix) and len(word) > len(suffix) + 1:
            stem = word[: -len(suffix)]
            out.update(stem + tail for tail in replacements)
            if suffix in ("ed", "ing") and len(stem) > 2 and stem[-1] == stem[-2]:
                out.add(stem[:-1])
    return frozenset(out)


def content_words(tokens: Sequence[str], *, vocabulary: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """The turn's content words, in order: every word that is not a function
    word, a pointing word, a number, a governed time word, in
    `TASK_VOCABULARY` or in `vocabulary` (the vault's referential cue and
    filler words)."""
    words = _words(tokens)
    return tuple(
        word
        for index, word in enumerate(words)
        if any(ch.isalpha() for ch in word)
        and not (word_forms := forms(word)) & _KNOWN
        and not word_forms & vocabulary
        and not (word_forms & _TIME_WORDS and index and words[index - 1] in _TIME_GOVERNORS)
    )


def mentioned(tokens: Iterable[str]) -> frozenset[str]:
    """Every form of every word in earlier turns, for `content_words` to be
    checked against."""
    return frozenset(form for word in _words(tokens) for form in forms(word))


def points_back(
    tokens: Sequence[str], *, referential_cue: bool = False, follow_up_markers: frozenset[str] = frozenset()
) -> bool:
    """(c): does the turn carry a word that points at something said before?"""
    if referential_cue:
        return True
    words = _words(tokens)
    if any(tuple(words[: len(opener)]) == opener for opener in _ELLIPTICAL_OPENERS):
        return True
    markers = (follow_up_markers - BARE_POINTERS) | PERSONAL_ANAPHORS | DEMONSTRATIVES
    for index, word in enumerate(words):
        following = words[index + 1] if index + 1 < len(words) else ""
        # "this week", "these days", "next month" name a time, not a referent.
        names_a_time = bool(forms(following) & _TIME_WORDS) and following not in _TIME_ADVERBS
        if word in markers and not (word in _TIME_GOVERNORS and names_a_time):
            return True
    governors = DETERMINERS | DEMONSTRATIVES | ORDINALS
    return any(
        (right in BARE_POINTERS and left in governors) or (left in ORDINALS and right in ORDINAL_HEADS)
        for left, right in zip(words, words[1:])
    )
