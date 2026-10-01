"""When a turn may take its subject from the conversation (thread-aware
compilation, ruling C1 on #1463, round 8).

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

A turn-local introduction, quotation or supplied alternative set selected by
a pointer vetoes carry before the content/title licence is checked.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .working_set_index import STOPWORDS, fold_plural, normalize, tokens_of

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
# Only surface words are neutral; inflectional stems cannot become grammar.
_KNOWN: frozenset[str] = _FUNCTION_WORDS | _POINTERS | _CLOCK_NUMBERS
# An opaque token outside any title/task licence, never a pointing word.
_CODE_SPAN = "`code`"
_BACKTICKS = re.compile(r"`+")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)[\r\n]*$")
_BLOCKQUOTE = re.compile(r" {0,3}> ?")
_LIST_MARKER = re.compile(r" {0,3}(?:[-*+]|\d{1,9}[.)]) {1,4}(?=\S|$)")
_BLOCK_START = re.compile(
    r" {0,3}(?:>|[-*+](?:\s|$)|\d{1,9}[.)](?:\s|$)|`{3,}|~{3,}"
    r"|#{1,6}(?:\s|$)|<[/!?a-zA-Z]|(?:[-*_]\s*){3,}$|=+\s*$)"
)
_SEPARATORS = re.compile(r"(->|→|[/|.!?;:=,\n\r\u2013\u2014])")
_PUNCTUATION = frozenset("/|.!?;:=,\n\r–—") | {"->", "→"}
_INTRODUCERS = frozenset("a an some any another several many few both either neither each every all much enough".split())
_NOVEL = frozenset("new fresh additional".split())
_POSSESSION = frozenset("have has had get got gotten found bought received acquired brought own owns make made give gave given".split())
_LOCAL_SUBJECTS = frozenset("i we you he she they someone somebody".split())
_NEUTRAL_HEADS = frozenset("one ones person people something anything someone somebody".split())
_LOCAL_MODIFIERS = frozenset("just already now recently finally also still".split())
_AUXILIARIES = _COPULAS | frozenset("have has had do does did will would shall should can could may might must".split())
_PREPOSITIONS = frozenset("about above across after against along among around as at before behind below beside between beyond by down during except for from in inside into near of off on onto out outside over past since than through till to toward under until up upon via with within without".split())
_POSSESSIVES = frozenset("my our your his her their its".split())
_NOMINAL_MODIFIERS = _NOVEL | ORDINALS | frozenset("current final latest more most less least very quite too really".split())
_DISCOURSE = _LOCAL_MODIFIERS | frozenset("so well please then".split())
_WH_WORDS = frozenset("what who whom whose which where when why how".split())
_SELECTORS = frozenset("which choose pick prefer compare".split())
_SUBORDINATORS = frozenset("if because although though while whether unless when where once since until that".split())


def anaphora_tokens(text: str) -> tuple[str, ...]:
    """Read code and quoted expressions as opaque tokens, never pointers."""
    return surface_analysis(text)[0]


def _continue_containers(line: str, containers: Sequence[int]) -> tuple[str, int]:
    # Zero denotes a quote marker; a list contributes its content indentation.
    for index, width in enumerate(containers):
        if width:
            if line.startswith(" " * width):
                line = line[width:]
            elif line.strip():
                return line, index
        else:
            quote = _BLOCKQUOTE.match(line)
            if quote is None:
                return line, index
            line = line[quote.end():]
    return line, len(containers)


def _opaque_spans(text: str) -> tuple[list[tuple[int, int, str]], bool]:
    spans: list[tuple[int, int, str]] = []
    lines: dict[int, tuple[str, int]] = {}
    offset = 0
    for line in text.splitlines(keepends=True):
        lines[offset] = (line.expandtabs(4), offset + len(line))
        offset += len(line)

    # The first delimiter owns its span, including any later fence-looking
    # line that actually closes inline code. Inline runs match exactly;
    # apostrophes inside words or after possessive nouns cannot open quotes.
    quoted = False
    index = 0
    containers: list[int] = []
    paragraph = False
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»"}
    unclosed_quotes: set[str] = set()
    while index < len(text):
        if index in lines:
            line, _line_end = lines[index]
            line, continued = _continue_containers(line, containers)
            # Paragraphs may continue lazily without repeating a container's
            # indentation. Code bodies below must repeat it on every line.
            if not (paragraph and line.strip() and not _BLOCK_START.match(line)):
                containers = containers[:continued]
            while marker := _BLOCKQUOTE.match(line) or _LIST_MARKER.match(line):
                containers.append(0 if ">" in marker[0] else marker.end())
                line = line[marker.end():]
            paragraph = bool(line.strip()) and not _BLOCK_START.match(line)
            opening = _FENCE.fullmatch(line)
            if opening and (opening[1][0] != "`" or "`" not in opening[2]):
                end = len(text)
                for start, (body, line_end) in lines.items():
                    if start <= index:
                        continue
                    body, continued = _continue_containers(body, containers)
                    if continued < len(containers):
                        end = start
                        break
                    closing = _FENCE.fullmatch(body)
                    if (closing and closing[1][0] == opening[1][0]
                            and len(closing[1]) >= len(opening[1]) and not closing[2].strip()):
                        end = line_end
                        break
                spans.append((index, end, _CODE_SPAN))
                index = end
                continue
        ch = text[index]
        if ch == "`":
            opening = _BACKTICKS.match(text, index)
            assert opening is not None
            closing = next((
                run for run in _BACKTICKS.finditer(text, opening.end())
                if len(run[0]) == len(opening[0])
            ), None)
            if closing is not None:
                spans.append((index, closing.end(), _CODE_SPAN))
                index = closing.end()
                continue
            index = opening.end()
            continue
        if ch in pairs and ch not in unclosed_quotes and not (
            ch == "'" and index > 0 and text[index - 1].isalnum()
        ):
            end = index + 1
            while end < len(text):
                if text[end] == pairs[ch] and not (
                    ch in {"'", "‘"} and end + 1 < len(text)
                    and text[end - 1].isalnum() and text[end + 1].isalnum()
                ):
                    spans.append((index, end + 1, "`quote`"))
                    quoted = True
                    index = end + 1
                    break
                end += 1
            else:
                # No valid closer remains for this delimiter anywhere in the
                # suffix. Later openers cannot change that result.
                unclosed_quotes.add(ch)
                index += 1
            continue
        index += 1
    return sorted(spans), quoted


@dataclass
class _Nominal:
    start: int
    end: int
    role: str
    head: str = ""
    pointer: bool = False
    quantified: bool = False
    possessive: str = ""
    relative: bool = False
    historical: bool = False
    ordinal: bool = False
    members: int = 1


@dataclass
class _Construction:
    start: int
    end: int
    nominals: list[_Nominal]
    finite: bool = False
    question: bool = False
    elliptical: bool = False
    request: bool = False
    selector: bool = False


class _ConstructionScanner:
    """A monotone cursor over shallow spans, never a search of every suffix."""

    def __init__(self, visible: str) -> None:
        self.words: list[str] = []
        for line in visible.splitlines(keepends=True):
            marker = _LIST_MARKER.match(line)
            for part in _SEPARATORS.split(line[marker.end():] if marker else line):
                if part in _PUNCTUATION:
                    self.words.append(part)
                else:
                    for token in tokens_of(part):
                        if token.endswith("'s") and token[:-2] not in _FUNCTION_WORDS:
                            self.words.append(token)
                        else:
                            self.words.extend(_words((token,)))

    def _nominal(self, start: int, role: str) -> _Nominal:
        words = self.words
        span = _Nominal(start, start, role)
        governed = start > 0 and words[start - 1] in DETERMINERS | DEMONSTRATIVES | ORDINALS
        complement = False
        member = False
        while span.end < len(words):
            index = span.end
            word = words[index]
            following = words[index + 1] if index + 1 < len(words) else ""
            if word in _PUNCTUATION or word in _AUXILIARIES:
                break
            if (span.head and (role != "copular" or governed) and not complement
                    and following and following not in _PUNCTUATION and (
                        word in {"that", "which", "who", "whom", "whose"} or word in _LOCAL_SUBJECTS
                    )
            ):
                # A relative belongs to this noun, including its coordinated
                # subjects and embedded predicates. Its description is local
                # evidence regardless of which verb the writer chose.
                span.relative = True
                while span.end < len(words) and words[span.end] not in _PUNCTUATION:
                    span.end += 1
                break
            if word in _SUBORDINATORS and span.end > start and not (
                complement and not span.head and word in DEMONSTRATIVES
            ):
                break
            if word in {"and", "or", "but"}:
                if (word == "but" or role == "copular" and not governed
                        or following in _AUXILIARIES | _LOCAL_SUBJECTS | {"it", "not"}):
                    break
                if span.historical or span.pointer:
                    break
                member = bool(span.head)
                span.end += 1
                continue
            if word in _PREPOSITIONS:
                if word == "of" and (span.head or span.quantified or span.ordinal):
                    complement = True
                    span.end += 1
                    continue
                if span.head and word in {"on", "about", "from", "for", "with"} and (
                    following in PERSONAL_ANAPHORS | DEMONSTRATIVES
                ):
                    span.historical = True
                    span.end += 2
                    continue
                if span.historical and word in _ADJUNCT_PREPOSITIONS:
                    tail = index + 1
                    while tail < len(words) and words[tail] in _COMPLEMENT_MODIFIERS:
                        tail += 1
                    if tail < len(words) and words[tail] in _TIME_WORDS:
                        span.end = tail + 1
                        continue
                break
            if span.historical:
                if word in _DISCOURSE | _TIME_ADVERBS:
                    span.end += 1
                    continue
                break
            if span.pointer and not complement:
                break
            if word in _POSSESSIVES or word.endswith("'s"):
                if span.head and not complement and not member:
                    break
                span.possessive = span.possessive or word
                governed = True
                span.end += 1
                continue
            if word in {"the", "a", "an"} | DEMONSTRATIVES | _INTRODUCERS:
                if span.head and not complement and not member:
                    break
                span.quantified |= word in _INTRODUCERS
                governed = True
                if word in DEMONSTRATIVES and following not in _NOMINAL_MODIFIERS | _POSSESSIVES and (
                    following in _AUXILIARIES | _PREPOSITIONS | _PUNCTUATION | _LOCAL_SUBJECTS or not following
                ):
                    span.pointer = True
                span.end += 1
                continue
            if word in _NOMINAL_MODIFIERS | _DISCOURSE:
                span.quantified |= word in _NOVEL
                span.ordinal |= word in ORDINALS
                span.end += 1
                continue
            if word in PERSONAL_ANAPHORS | _LOCAL_SUBJECTS | {"me", "us", "you"}:
                if span.head:
                    break
                span.pointer = True
                span.end += 1
                break
            if word in _WH_WORDS or word in {"not", "no"}:
                break
            if word in BARE_POINTERS and (governed or span.ordinal) and not complement:
                span.pointer = not span.quantified
                if span.quantified:
                    span.head = word
                span.end += 1
                break
            if word == "one" and following == "of":
                span.quantified = True
                span.end += 1
                continue
            if member:
                span.members += 1
                member = False
            # The grammatical slot licenses a head, including function words
            # such as "end"; _KNOWN is a lexical gate, not a noun dictionary.
            if span.head.isdecimal() or span.head in _CLOCK_NUMBERS:
                span.quantified = True
                span.head = word
            elif not span.head or complement:
                span.head = word
            complement = False
            span.end += 1
        return span

    def _construction(self, start: int) -> _Construction:
        words = self.words
        span = _Construction(start, start, [])
        index = start
        while index < len(words) and words[index] in _DISCOURSE:
            index += 1
        content = index
        opener = tuple(words[index:index + 2])
        span.elliptical = opener in {("what", "about"), ("how", "about")}
        if span.elliptical:
            index += 2
        elif index < len(words) and words[index] in _WH_WORDS:
            span.question = True
            index += 1
        if index < len(words) and words[index] in _AUXILIARIES:
            following = words[index + 1] if index + 1 < len(words) else ""
            inverted = following in (
                _LOCAL_SUBJECTS | PERSONAL_ANAPHORS | DEMONSTRATIVES | _POSSESSIVES
                | _INTRODUCERS | {"the", "there"}
            )
            span.question |= inverted
            if inverted:
                span.finite = True
                index += 1
        role = "object" if span.elliptical else "subject"
        subject: _Nominal | None = None
        object_seen = False
        while index < len(words) and words[index] not in _PUNCTUATION | {"and", "or", "but"}:
            word = words[index]
            if word in _DISCOURSE | {"not"}:
                index += 1
                continue
            if word in _PREPOSITIONS:
                following = words[index + 1] if index + 1 < len(words) else ""
                role = "subject" if following in _LOCAL_SUBJECTS | {"it"} else "adjunct"
                if word == "to" and index + 2 < len(words) and following not in (
                    _INTRODUCERS | _POSSESSIVES | DEMONSTRATIVES | PERSONAL_ANAPHORS
                    | _AUXILIARIES | _PREPOSITIONS | _NOMINAL_MODIFIERS | {"the"}
                ) and words[index + 2] in DEMONSTRATIVES:
                    tail = index + 3
                    if tail < len(words) and words[tail] in _PREPOSITIONS:
                        tail += 1
                    if tail == len(words) or words[tail] in _PUNCTUATION | {"and", "or", "but"}:
                        role = "predicate"
                if role == "subject":
                    subject = None
                    object_seen = False
                index += 1
                continue
            if word in _SUBORDINATORS:
                role = "subject"
                subject = None
                object_seen = False
                index += 1
                continue
            if word in _AUXILIARIES or role == "predicate" and word not in (
                _INTRODUCERS | _POSSESSIVES | DEMONSTRATIVES | {"the"}
            ):
                span.finite = True
                following = words[index + 1] if index + 1 < len(words) else ""
                perfect = word in {"have", "has", "had"} and following not in _NEUTRAL_HEADS and (
                    following.endswith(("ed", "ing")) or following in {
                        "been", "done", "seen", "heard", "known", "chosen", "made", "got", "gotten", "bought", "found", "brought",
                    }
                )
                progressive = word in _COPULAS and following.endswith("ing") and following not in _TIME_WORDS | _NEUTRAL_HEADS
                if perfect or progressive or word in _AUXILIARIES - _COPULAS - {"have", "has", "had"}:
                    role = "predicate"
                elif word in _COPULAS:
                    role = "presentation" if subject and words[subject.start] in {"here", "there"} else "copular"
                else:
                    role = "acquired" if word in _POSSESSION else "object"
                index += 1
                continue
            nominal = self._nominal(index, role)
            if nominal.end == index:
                index += 1
                continue
            if object_seen and role == "object" and nominal.head in {"way", "manner"}:
                nominal.role = "manner"
            object_seen |= role in {"object", "acquired"}
            span.nominals.append(nominal)
            index = nominal.end
            if role == "subject":
                subject = nominal
                role = "predicate"
            elif role == "copular":
                role = "object"
        span.end = index
        span.question |= index < len(words) and words[index] == "?"
        span.selector = any(word in _SELECTORS for word in words[start:index])
        if len(span.nominals) == 1:
            nominal = span.nominals[0]
            span.request = (
                not span.finite and nominal.start == content and nominal.historical
                and words[nominal.start] in _INTRODUCERS - {"a", "an", "another", "both", "either", "neither", "each", "every"}
            )
        return span

    @staticmethod
    def _introduces_local(span: _Construction) -> bool:
        subject = next((nominal for nominal in span.nominals if nominal.role == "subject"), None)
        for nominal in span.nominals:
            if nominal.relative and nominal.head:
                return True
            if nominal.role == "manner":
                continue
            if nominal.quantified and (nominal.head or not span.question) and not nominal.pointer and not span.request:
                return True
            if (nominal.possessive and nominal.possessive != "its" and nominal.role != "adjunct"
                    and not span.question and not span.elliptical):
                return True
            if nominal.head and not nominal.pointer and nominal.role in {"acquired", "presentation"} and not (
                span.question and nominal.possessive
            ):
                return True
            if nominal.role == "copular" and subject and subject.head and not subject.pointer and (
                nominal.head.isdecimal() or nominal.head in _TIME_WORDS | _CLOCK_NUMBERS | {"here", "there", "above", "below"}
            ):
                return True
        return False

    def local_material(self) -> str:
        index = 0
        previous: _Construction | None = None
        separator = ""
        members = 0
        selected = False
        while index < len(self.words):
            if self.words[index] in _PUNCTUATION | {"and", "or", "but"}:
                separator = self.words[index]
                index += 1
                continue
            span = self._construction(index)
            if self._introduces_local(span):
                return "introduced"
            value = not span.question and not span.request and (
                span.finite or any(nominal.head and not nominal.pointer for nominal in span.nominals)
            )
            if previous and separator in {":", "=", "->", "→"} and not previous.question and value and any(
                nominal.head or nominal.ordinal for nominal in previous.nominals if nominal.role == "subject"
            ):
                return "introduced"
            if span.selector:
                if members >= 2:
                    return "alternatives"
                selected = span.end < len(self.words) and self.words[span.end] in {":", "—", "–", "\n"}
                members = 0
            elif value:
                if members and (
                    separator not in {"/", "|", "or", "and", ",", "\n", "\r"}
                    or separator in {"and", "or"} and (span.finite or previous and previous.finite)
                ):
                    members = 0
                    selected = False
                members += max((nominal.members for nominal in span.nominals), default=1)
                if selected and members >= 2:
                    return "alternatives"
            else:
                members = 0
                selected = False
            previous = span
            index = max(index + 1, span.end)
        return ""


def surface_analysis(text: str) -> tuple[tuple[str, ...], str]:
    """Pointer tokens and a categorical turn-local veto, before lexical erasure."""
    spans, quoted = _opaque_spans(text)
    tokens: list[str] = []
    surface: list[str] = []
    start = 0
    for left, right, token in spans:
        tokens.extend(tokens_of(normalize(text[start:left])))
        tokens.append(token)
        surface.extend((text[start:left], " " + "\n" * text[left:right].count("\n")))
        start = right
    tokens.extend(tokens_of(normalize(text[start:])))
    surface.append(text[start:])
    if quoted:
        return tuple(tokens), "quotation"
    return tuple(tokens), _ConstructionScanner(normalize("".join(surface))).local_material()


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
        and word not in _KNOWN
        and not (word_forms := forms(word)) & _TASK_FORMS
        # Neutral grammar words in vault filler still use surface matching.
        and not word_forms & (vocabulary - _KNOWN)
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
