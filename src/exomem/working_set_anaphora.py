"""When a turn may take its subject from the conversation (thread-aware
compilation, ruling C1 on #1463, round 6).

The conversation carry runs for a turn that (a) resolves no anchor of its own,
which the compiler decides, and here:

* (c) POINTS BACK: a pronoun or possessive, a demonstrative, a shipped
  follow-up marker, an ordinal followed by "one" or "option", a governed bare
  pointer ("that one", "the other one", "which one"), an elliptical "what
  about ..." opener, or the vault's referential cue;
* (b) LICENSED CONTENT: every content word belongs to the carried subject's
  own name/title or the frozen generic task vocabulary below. A word merely
  shared with an earlier turn supplies no licence. Function words, pointing
  words, numbers, time words and the vault's referential vocabulary are neutral.
  An unlicensed content word is a topic switch:
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
    noon midnight midday dawn dusk sunrise sunset equinox clock oclock
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
#: These governors make a demonstrative temporal deixis ("this week"),
#: rather than a pointing word. Time words themselves are neutral.
_TIME_GOVERNORS: frozenset[str] = frozenset(
    """next last this these previous coming following every on by in for
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
_CLITIC_VERBS: dict[str, str] = {"s": "is", "re": "are", "ll": "will", "d": "would", "ve": "have", "m": "am"}
#: "n't" stems that are not the verb minus its final "n".
_NEGATED: dict[str, str] = {"won": "will", "can": "can", "shan": "shall", "ain": "is"}


def _words(tokens: Iterable[str]) -> list[str]:
    """Tokens with contractions split ("it's" -> "it", "is"; "isn't" -> "isn",
    "not"), a possessive folded to its noun, and hyphenated words split."""
    words: list[str] = []
    for token in tokens:
        if token == "let's":
            words.extend(("let", "us"))
            continue
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


#: Normalize the closed task vocabulary exactly as the turn's words. The
#: vocabulary entries themselves stay frozen; e.g. "figures" also covers "figure".
_TASK_FORMS: frozenset[str] = frozenset(form for word in TASK_VOCABULARY for form in forms(word))
_TIME_FORMS: frozenset[str] = frozenset(form for word in _TIME_WORDS for form in forms(word))
#: Grammar for a dummy subject's bare copular complement. Work predicates
#: ("ready", "due") stay outside it; these are not task vocabulary additions.
_COPULAS = frozenset("am is are was were be been being".split())
_CLOCK_NUMBERS = frozenset(
    "one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty".split()
)
_KNOWN: frozenset[str] = _FUNCTION_WORDS | _TASK_FORMS | _POINTERS | _CLOCK_NUMBERS
_ADJUNCT_PREPOSITIONS = frozenset("on in at after before by for from until during".split())
_COMPLEMENT_MODIFIERS = frozenset(
    "not still now then yet already nearly almost about around the a an "
    "next last this these previous coming following".split()
)
_WEATHER_HEADS = frozenset(
    "rain snow snowy sleet hail drizzle windy foggy sunny cloudy cold hot warm cool "
    "wet dry humid freezing frosty stormy dark".split()
)
_DISTANCE_HEADS = frozenset("far near uphill downhill mile kilometer kilometre meter metre foot feet inch".split())
_DATE_CLOCK_HEADS = _TIME_WORDS | _CLOCK_NUMBERS | frozenset(
    "start end beginning middle holiday half am pm".split()
)
_CLOSING_IDIOMS = (
    ("leave", "it", "there"),
    ("leave", "it"),
    ("that", "is", "it"),
    ("forget", "it"),
    ("drop", "it"),
    ("call", "it", "a", "day"),
    ("that", "will", "do", "it"),
)
_ACKNOWLEDGEMENT_WORDS = frozenset(
    "ok okay fine right yes yeah yep well thanks thank you that's that is all "
    "enough never mind let us please just now for today then and there".split()
)


def _closing(words: Sequence[str]) -> bool:
    """A closed idiom plus only acknowledgement/closing words ends the turn.
    A task-bearing remainder ("drop it from the plan") keeps its referent."""
    for idiom in _CLOSING_IDIOMS:
        for index in range(len(words) - len(idiom) + 1):
            if tuple(words[index : index + len(idiom)]) == idiom:
                remainder = (*words[:index], *words[index + len(idiom) :])
                if all(word in _ACKNOWLEDGEMENT_WORDS for word in remainder):
                    return True
    return False


def _dummy_time_it(words: Sequence[str], index: int) -> bool:
    """A bare time/date/clock/weather/distance complement makes copular it
    dummy. A preposition heads an adjunct of a real referent instead.
    Statements, questions, negations and contractions share this structure."""
    start = index + 1
    while start < len(words) and words[start] in {"not", "will", "would", "still", "has", "have", "had"}:
        start += 1
    if start < len(words) and words[start] in _COPULAS:
        start += 1
    else:
        before = index - 1
        if before >= 0 and words[before] == "not":
            before -= 1
        if before < 0 or words[before] not in _COPULAS:
            return False
    while start < len(words) and words[start] in _COMPLEMENT_MODIFIERS:
        start += 1
    complement = words[start:]
    # In wh-questions the bare complement precedes the inverted copula.
    fronted = tuple(words[:2]) in {
        ("what", "time"), ("what", "day"), ("what", "date"),
    } or (len(words) > 1 and words[0] == "how"
          and bool(forms(words[1]) & (_WEATHER_HEADS | _DISTANCE_HEADS)))
    if not complement:
        return fronted
    head = complement[0]
    if head in _ADJUNCT_PREPOSITIONS or (fronted and head in {"to", "outside", "there", "here"}):
        return fronted
    # "the one from last week" / "the first on Tuesday": the pointer is
    # the head, with a temporal prepositional adjunct, rather than a date.
    if (head in ORDINAL_HEADS | ORDINALS
            and len(complement) > 1 and complement[1] in _ADJUNCT_PREPOSITIONS):
        return False
    if head in ORDINALS:
        # Ordinals name a date only with its "of <time>" complement;
        # otherwise they select a real item ("the first", "second option").
        return (len(complement) > 2 and complement[1] == "of"
                and any(forms(word) & _TIME_WORDS for word in complement[2:]))
    if head in ORDINAL_HEADS and "the" in words[index + 1 : start]:
        return False
    return bool(forms(head) & (_DATE_CLOCK_HEADS | _WEATHER_HEADS | _DISTANCE_HEADS)) or head.isdecimal()


def content_words(tokens: Sequence[str], *, vocabulary: frozenset[str] = frozenset()) -> tuple[str, ...]:
    """The turn's content words, in order: every word that is not a function
    word, a pointing word, a number, a neutral time word, in
    `TASK_VOCABULARY` or in `vocabulary` (the vault's referential cue and
    filler words)."""
    words = _words(tokens)
    return tuple(
        word
        for word in words
        if any(ch.isalpha() for ch in word)
        and not (word_forms := forms(word)) & _KNOWN
        and not word_forms & vocabulary
        and not word_forms & _TIME_FORMS
    )


def points_back(
    tokens: Sequence[str], *, referential_cue: bool = False, follow_up_markers: frozenset[str] = frozenset()
) -> bool:
    """(c): does the turn carry a word that points at something said before?"""
    words = _words(tokens)
    if _closing(words) or any(
        word == "it" and _dummy_time_it(words, index) for index, word in enumerate(words)
    ):
        return False
    if referential_cue:
        return True
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
