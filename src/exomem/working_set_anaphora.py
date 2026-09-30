"""The shipped anaphor grammar the conversation carry reads (thread-aware
compilation, ruling C1 on #1463).

Whether a turn leans on something said before is a question of English
grammar, not of a vault's vocabulary: the spec defines the anaphor set as a
closed, SHIPPED set, and these tables are its parts of speech (pronouns,
demonstratives, prepositions, auxiliaries, temporal nouns). A vault's
referential cues and filler stay vault data in the activation conventions;
none of them is listed here (`tests/test_activation_conventions_referential.py`
pins that). `working_set_resolve.is_anaphoric` is the one caller.
"""

from __future__ import annotations

from collections.abc import Sequence

from .working_set_index import STOPWORDS

#: The closed anaphor set beyond the shipped follow-up markers and the vault's
#: referential cues (`TurnAnalysis.referential_cue`): personal pronouns and
#: possessives, and the demonstratives, kept as the two grammatical classes they
#: are. "the former" and "the latter" are follow-up markers already.
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

# --- Pronoun-bearing turns that do not point back -------------------------- #
# A pronoun or demonstrative is an anaphor only where it refers. Three closed,
# grammatical classes do not, and are excluded (orchestrator ruling C1 on
# #1463): an expletive or dummy "it" ("is it possible to...", "it's raining",
# "it seems that"), a complementiser or relative "that" ("I think that we
# should...", "the recipe that my aunt sent"), and temporal deixis ("this
# week", "the other day", "next week"). An "it" whose antecedent is an earlier
# clause of the SAME turn ("the grocery list, can you make it shorter?") and a
# closing acknowledgement ("thanks, that's all") point back at nothing the
# conversation carry could supply either. Contractions are split first, so
# "it's still on track?" and "that's what I meant" are read as the pronoun
# they are.

#: A contraction's clitic, read as the verb it stands for.
_CLITIC_VERBS: dict[str, str] = {"s": "is", "re": "are", "ll": "will", "d": "would", "ve": "have", "m": "am"}
#: "n't" contractions whose stem is not the verb minus its final "n".
_NEGATED_STEMS: dict[str, str] = {"can": "can", "won": "will", "shan": "shall", "ain": "is"}
#: Temporal nouns: a deictic in front of one names a time, not a subject.
TEMPORAL_NOUNS: frozenset[str] = frozenset(
    {
        "morning", "afternoon", "evening", "night", "day", "days", "week", "weeks", "weekend", "weekends", "fortnight",
        "month", "months", "year", "years", "quarter", "term", "semester", "season",
        "spring", "summer", "autumn", "fall", "winter", "time", "times", "hour", "minute",
        "moment", "decade", "century", "sprint", "monday", "tuesday", "wednesday",
        "thursday", "friday", "saturday", "sunday",
    }
)
#: Deictics that name a time when a temporal noun follows ("this week",
#: "these days", "next week", "last year").
_TEMPORAL_DEICTICS: frozenset[str] = frozenset({"this", "these", "next", "last", "earlier"})
#: Time adverbs: "earlier today", but never "this today" ("revisit this
#: tomorrow" keeps its pronoun).
_TIME_ADVERBS: frozenset[str] = frozenset({"today", "tonight", "tomorrow", "yesterday"})
_COPULAS: frozenset[str] = frozenset({"is", "was", "be", "been", "being", "are", "were"})
#: Auxiliaries that open a question with "it" behind them ("is it possible
#: to...", "would it be okay to...", "does it matter if...").
_QUESTION_AUXILIARIES: frozenset[str] = frozenset(
    {"is", "was", "would", "will", "could", "might", "does", "did", "do", "should", "can"}
)
#: Modal or perfect material between "it" and its predicate: "it will be",
#: "it has been", "it might not be".
_VERB_CHAIN: frozenset[str] = frozenset(
    {"will", "would", "could", "might", "may", "must", "should", "can", "has", "had", "have", "not"}
)
_DEGREE_ADVERBS: frozenset[str] = frozenset(
    {
        "really", "still", "even", "also", "ever", "not", "too", "very", "so", "more",
        "less", "actually", "already", "always", "usually", "generally", "quite",
        "pretty", "rather", "much", "just", "probably", "definitely", "certainly", "getting",
        "nearly", "almost", "fairly", "totally", "absolutely", "hardly", "barely",
    }
)
_PREPOSITIONS: frozenset[str] = frozenset(
    {
        "on", "in", "at", "for", "with", "by", "of", "off", "up", "down", "over", "under",
        "about", "into", "onto", "from", "through", "behind", "ahead", "out", "like", "as",
        "than", "to", "around", "after", "before",
    }
)
#: What an extraposed subject clause opens with: "is it possible TO...", "it
#: is clear THAT...", "does it matter IF...".
_EXTRAPOSITION_MARKERS: frozenset[str] = frozenset(
    {"to", "that", "whether", "if", "how", "when", "where", "why", "what", "who", "which"}
)
#: Predicates of weather and clock time: "it's raining", "it is sunny".
_WEATHER_WORDS: frozenset[str] = frozenset(
    {
        "raining", "snowing", "pouring", "drizzling", "sunny", "cloudy", "windy", "foggy",
        "misty", "stormy", "cold", "hot", "warm", "freezing", "chilly", "humid", "dark",
        "midnight", "noon", "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday", "morning", "evening",
    }
)
_WEATHER_VERBS: frozenset[str] = frozenset({"rains", "rained", "snows", "snowed", "pours", "poured"})
#: Raising verbs whose "it" is a placeholder: "it seems that", "it turns out".
_RAISING_VERBS: frozenset[str] = frozenset(
    {
        "seems", "seemed", "appears", "appeared", "looks", "looked", "sounds", "sounded",
        "feels", "felt", "turns", "turned", "happens", "happened",
    }
)
_RAISING_COMPLEMENTS: frozenset[str] = frozenset({"that", "like", "to", "out", "as"})
#: "make it shorter", "keep it simple": an instruction about the answer's own
#: text, never a subject the conversation named.
_STYLE_ADJECTIVES: frozenset[str] = frozenset(
    {
        "short", "simple", "brief", "clear", "concise", "formal", "casual", "quick",
        "tight", "snappy", "friendly", "polite", "plain", "neat", "tidy", "readable",
    }
)
#: Words that cannot head the noun phrase a determiner opens.
_FUNCTION_WORDS: frozenset[str] = STOPWORDS | PERSONAL_ANAPHORS | DEMONSTRATIVES | BARE_POINTERS
#: Clause openers a same-turn antecedent must sit before.
_COORDINATORS: frozenset[str] = frozenset({"and", "but", "so", "or", "because", "then"})
#: Determiners that introduce a noun phrase an "it" may point back to.
_NP_DETERMINERS: frozenset[str] = frozenset(
    {"the", "a", "an", "my", "our", "your", "every", "each", "any", "some", "no"}
)
#: Words after which "that" introduces a clause, not a thing: verbs of
#: saying, thinking and knowing, their adjectives and the subordinators.
_COMPLEMENT_TAKERS: frozenset[str] = frozenset(
    {
        "think", "thinks", "thought", "believe", "believes", "believed", "hope", "hopes",
        "hoped", "guess", "guessed", "reckon", "reckons", "reckoned", "suppose", "supposed",
        "assume", "assumed", "know", "knows", "knew", "known", "say", "says", "said",
        "feel", "feels", "felt", "realise", "realised", "realize", "realized", "suspect",
        "suspected", "agree", "agreed", "doubt", "wish", "wished", "claim", "claims",
        "claimed", "admit", "admitted", "bet", "figure", "figured", "read", "hear", "heard",
        "learn", "learned", "learnt", "mean", "meant", "decide", "decided", "confirm",
        "confirmed", "mention", "mentioned", "explain", "explained", "note", "noted",
        "suggest", "suggested", "insist", "insisted", "ensure", "understand", "understood",
        "sure", "glad", "afraid", "aware", "certain", "convinced", "confident", "clear",
        "true", "likely", "possible", "obvious", "happy", "sorry", "surprised", "worried",
        "pleased", "so", "now", "given", "provided", "except", "assuming", "such",
    }
) | _RAISING_VERBS
#: Verbs of perception and discovery: their "that" is a complementiser only
#: when a noun phrase opens the clause ("I noticed that my plants..."), and
#: the demonstrative otherwise ("did you see that report?").
_PERCEPTION_VERBS: frozenset[str] = frozenset(
    {"notice", "see", "saw", "seen", "find", "found", "discover", "remember", "forget", "forgot", "show", "prove", "check"}
)
_POSSESSIVE_DETERMINERS: frozenset[str] = frozenset({"my", "our", "your", "his", "her", "their", "its"})


def _lemma_in(word: str, words: frozenset[str]) -> bool:
    """`word`, or its stem without a regular -s, -es, -ed or -d, is in `words`."""
    if word in words:
        return True
    for suffix in ("es", "s", "ed", "d"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2 and word[: -len(suffix)] in words:
            return True
    return False


#: Verbs whose "that" follows an indirect object: "she told me that...".
_TELLING_VERBS: frozenset[str] = frozenset(
    {"told", "tell", "tells", "showed", "show", "shows", "reminded", "remind", "warned", "assured", "promised"}
)
_OBJECT_PRONOUNS: frozenset[str] = frozenset({"me", "us", "him", "her", "them", "you"})
#: "that" followed by one of these is the pronoun itself ("I think that is
#: right", "I know that one", "I said that already").
_DEMONSTRATIVE_FOLLOWERS: frozenset[str] = _PREPOSITIONS | frozenset(
    {
        "is", "was", "are", "were", "will", "would", "could", "can", "should", "might",
        "must", "may", "has", "had", "have", "does", "did", "do", "sounds", "seems",
        "looks", "works", "makes", "means", "matters", "helps", "needs", "one", "ones",
        "already", "too", "again", "yesterday", "earlier", "and", "or", "but", "then",
        "because", "when", "now", "instead", "anyway", "though", "first", "part", "bit",
        "please", "not",
    }
)
#: A subject pronoun after "that" makes it a complementiser or relative
#: pronoun ("that we should", "the page that it links to", "is that it").
_SUBJECT_PRONOUNS: frozenset[str] = frozenset({"i", "we", "you", "they", "he", "she", "it", "there"})
#: A closing acknowledgement: "that's all", "that's fine, thanks", "it's ok".
_CLOSING_WORDS: frozenset[str] = frozenset(
    {
        "all", "it", "fine", "great", "ok", "okay", "perfect", "cool", "lovely", "brilliant",
        "enough", "right", "true", "good", "helpful", "useful", "everything", "awesome",
        "excellent", "grand", "super", "wonderful",
    }
)
_CLOSING_TAIL: frozenset[str] = frozenset(
    {"for", "today", "now", "thanks", "thank", "you", "cheers", "then", "so", "much", "the", "help", "mate", "really"}
)
#: Ellipsis: "what about X?" reuses the previous question's predicate.
_ELLIPTICAL_OPENERS: tuple[tuple[str, ...], ...] = (
    ("what", "about"),
    ("how", "about"),
    ("and", "what", "about"),
    ("and", "how", "about"),
)


def _split_clitics(
    tokens: Sequence[str], breaks: frozenset[int]
) -> tuple[tuple[str, ...], tuple[bool, ...]]:
    """`tokens` with every contraction split into its word and the verb its
    clitic stands for ("it's" -> "it", "is"; "isn't" -> "is", "not"), a
    possessive folded to its noun, and for each word whether a clause starts
    there (a punctuation break in `breaks`, which indexes `tokens`)."""
    words: list[str] = []
    starts: list[bool] = []
    for index, token in enumerate(tokens):
        head, apostrophe, tail = token.partition("'")
        pieces: tuple[str, ...] = (token,)
        if apostrophe and head:
            if tail == "t" and head.endswith("n"):
                stem = _NEGATED_STEMS.get(head, head[:-1])
                pieces = (stem, "not")
            elif tail in _CLITIC_VERBS and head in _CLITIC_HOSTS:
                pieces = (head, _CLITIC_VERBS[tail])
            else:
                pieces = (head,)
        for position, piece in enumerate(pieces):
            words.append(piece)
            starts.append(position == 0 and index in breaks)
    return tuple(words), tuple(starts)


#: The words a clitic verb attaches to. Any other "X's" is a possessive.
_CLITIC_HOSTS: frozenset[str] = frozenset(
    {
        "it", "that", "this", "there", "here", "what", "who", "where", "how", "when", "why",
        "he", "she", "they", "we", "you", "i", "let", "those", "these",
    }
)


def _at(words: Sequence[str], index: int) -> str:
    return words[index] if 0 <= index < len(words) else ""


def _predicate_is_extraposed(words: Sequence[str], index: int, *, lexical: bool = False) -> bool:
    """Starting at the predicate after "it" + copula: a weather or clock word,
    or an adjective or short noun phrase followed by a subject clause.

    `lexical`: the predicate is a verb ("does it matter if", "it doesn't
    matter whether"), where a following "to" is the verb's own complement
    ("does it need to ship?") and never marks extraposition."""
    skipped = 0
    while _at(words, index) in _DEGREE_ADVERBS and skipped < 2:
        index += 1
        skipped += 1
    slot = _at(words, index)
    if not slot:
        return False
    if slot in _WEATHER_WORDS:
        return True
    if slot == "worth":
        return _at(words, index + 1).endswith("ing")
    if slot in ("a", "an"):
        return any(_at(words, index + step) in _EXTRAPOSITION_MARKERS for step in (2, 3))
    if slot in _PREPOSITIONS or slot in _SUBJECT_PRONOUNS or slot in _OBJECT_PRONOUNS:
        return False
    after = _at(words, index + 1)
    if lexical:
        return after in _EXTRAPOSITION_MARKERS and after != "to"
    if after == "for":
        return "to" in words[index + 2 : index + 5]
    return after in _EXTRAPOSITION_MARKERS


#: Do-support and modals: the predicate after them is a verb.
_DO_FORMS: frozenset[str] = frozenset({"does", "did", "do"})
_MODALS: frozenset[str] = frozenset({"would", "will", "could", "might", "should", "can", "may", "must"})


def _is_dummy_it(words: Sequence[str], index: int) -> bool:
    """An expletive "it": weather, clock time, extraposition, a raising verb,
    "it takes ... to", or an instruction about the answer's style."""
    after, before = _at(words, index + 1), _at(words, index - 1)
    if after in _WEATHER_VERBS or after in ("depends", "depended"):
        return True
    if after in _RAISING_VERBS and _at(words, index + 2) in _RAISING_COMPLEMENTS:
        return True
    if after in ("takes", "took", "take", "costs", "cost") and "to" in words[index + 2 : index + 6]:
        return True
    if before in ("make", "keep", "makes", "keeps") and (
        after in _STYLE_ADJECTIVES or (len(after) > 4 and after.endswith("er"))
    ):
        return True
    if after in _DO_FORMS:
        cursor = index + 2 + (_at(words, index + 2) == "not")
        return _predicate_is_extraposed(words, cursor, lexical=True)
    cursor = index + 1
    while _at(words, cursor) in _VERB_CHAIN:
        cursor += 1
    if _at(words, cursor) in _COPULAS:
        return _predicate_is_extraposed(words, cursor + 1)
    if before in _QUESTION_AUXILIARIES:
        if _at(words, index - 2) in ("time", "day", "date", "year", "month"):
            return True  # "what time is it"
        if after == "be":
            return _predicate_is_extraposed(words, index + 2)
        return _predicate_is_extraposed(
            words, index + 1, lexical=before in _DO_FORMS or before in _MODALS
        )
    return False


def _antecedent_in_turn(words: Sequence[str], starts: Sequence[bool], index: int) -> bool:
    """Does an earlier clause of the same turn introduce a noun phrase for
    this "it" to point to ("I bought a new kettle and it leaks")?"""
    boundary = max(
        (i for i in range(1, index + 1) if starts[i] or words[i] in _COORDINATORS),
        default=-1,
    )
    if boundary < 0:
        return False
    return any(
        words[k] in _NP_DETERMINERS and words[k + 1] not in _FUNCTION_WORDS
        for k in range(boundary - 1)
    )


def _is_closing(words: Sequence[str], index: int) -> bool:
    """ "that's all", "that's fine, thanks", "it's ok for now": an
    acknowledgement of the answer, not a question about a subject."""
    if _at(words, index + 1) not in ("is", "was") or _at(words, index + 2) not in _CLOSING_WORDS:
        return False
    return all(word in _CLOSING_TAIL for word in words[index + 3 :])


def _is_temporal(words: Sequence[str], index: int) -> bool:
    """A deictic that names a time: "this week", "these days", "next week",
    "earlier today", "the other day", "those days". "on the other hand" is
    an idiom and points nowhere either."""
    word, after = words[index], _fold_possessive_word(_at(words, index + 1))
    if word in _TEMPORAL_DEICTICS and after in TEMPORAL_NOUNS:
        return True
    if word == "earlier" and after in _TIME_ADVERBS:
        return True
    if word == "those" and after == "days":
        return True
    if word == "other":
        if tuple(words[max(0, index - 2) : index + 2]) == ("on", "the", "other", "hand"):
            return True
        return _at(words, index - 1) in ("the", "an") and after in TEMPORAL_NOUNS
    return False


def _fold_possessive_word(word: str) -> str:
    return word[:-2] if word.endswith("'s") else word


def _that_points(words: Sequence[str], starts: Sequence[bool], index: int) -> bool:
    """Is this "that" the demonstrative pronoun or determiner, rather than a
    complementiser ("I think that we should") or relative pronoun ("the
    recipe that my aunt sent")?"""
    after, before = _at(words, index + 1), _at(words, index - 1)
    if not after or (index + 1 < len(starts) and starts[index + 1]):
        return True  # "I said that." / "given that, ..."
    if after in _SUBJECT_PRONOUNS:
        return False
    if after in _DEMONSTRATIVE_FOLLOWERS:
        return True
    if _lemma_in(before, _COMPLEMENT_TAKERS):
        return False
    if _lemma_in(before, _PERCEPTION_VERBS) and after in _NP_DETERMINERS | _POSSESSIVE_DETERMINERS:
        return False
    if before in _OBJECT_PRONOUNS and _lemma_in(_at(words, index - 2), _TELLING_VERBS):
        return False
    for reach in (2, 3):
        if (
            _at(words, index - reach) in _NP_DETERMINERS
            and before not in _FUNCTION_WORDS
            and before not in _PREPOSITIONS
        ):
            return False  # a relative clause on a noun of this turn
    return True


def _points_back(
    words: Sequence[str], starts: Sequence[bool], index: int, markers: frozenset[str]
) -> bool:
    word = words[index]
    if word in PERSONAL_ANAPHORS:
        if word == "it" and (_is_dummy_it(words, index) or _is_closing(words, index)):
            return False
        if word in ("it", "its") and _antecedent_in_turn(words, starts, index):
            return False
        return True
    if word in DEMONSTRATIVES:
        if _is_temporal(words, index) or (word in ("that", "this") and _is_closing(words, index)):
            return False
        return word != "that" or _that_points(words, starts, index)
    if word in markers and word not in BARE_POINTERS:
        return not _is_temporal(words, index)
    return False


def is_anaphoric(
    tokens: Sequence[str],
    *,
    referential_cue: bool = False,
    breaks: frozenset[int] = frozenset(),
    follow_up_markers: frozenset[str] = frozenset(),
) -> bool:
    """Does a turn lean on something said before, of any length?

    True for a referential cue; an elliptical "what about ..." opener; a
    personal pronoun or demonstrative that refers (see the excluded classes
    above; contractions are split first); a shipped follow-up marker that does
    not name a time; an ordinal followed by "one" or "option"; or a bare
    pointer ("one", "ones", "other") that a determiner, demonstrative, ordinal
    or "which" governs, unless it names a time ("the other day"). A numeral use
    ("for one day") is not an anaphor. `breaks` are the token indices where
    clause punctuation falls (`TurnAnalysis.run_breaks`); `follow_up_markers`
    are the resolver's shipped follow-up markers. Whether the turn
    reached an anchor after all is the resolver's answer: the conversation
    carry it enables runs only for a turn that reached none."""
    if referential_cue:
        return True
    words, starts = _split_clitics(tokens, breaks)
    if any(words[: len(opener)] == opener for opener in _ELLIPTICAL_OPENERS):
        return True
    if any(
        _points_back(words, starts, index, follow_up_markers) for index in range(len(words))
    ):
        return True
    governors = DETERMINERS | DEMONSTRATIVES | ORDINALS
    for index in range(len(words) - 1):
        left, right = words[index], words[index + 1]
        if right in BARE_POINTERS and left in governors and not _is_temporal(words, index + 1):
            return True
        if left in ORDINALS and right in ORDINAL_HEADS:
            return True
    return False
