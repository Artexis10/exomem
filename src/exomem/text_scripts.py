"""Declared Unicode script data for lexical tokenization.

One table in one module: which code points belong to a script written without
spaces between words (scriptio continua), and which letters belong to the
scripts the lexical tokenizer stems or accent-folds. It is data about scripts,
declared as Unicode block ranges, never a word list or a language detector.
`character_tables` adds the combining marks, symbols and variation selectors
the tokenizer reads from the running interpreter's Unicode data.

`bm25.tokenize` reads it to decide where an unspaced run starts and which
Snowball stemmer, if any, a spaced word gets. Any other rule that needs to know
whether text is unspaced (a containment match over a CJK run, a chunk cap for
unspaced paragraphs) should read the same table rather than declare its own.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from functools import lru_cache

#: Blocks whose scripts are written without spaces between words: Han, kana,
#: Hangul, Thai, Lao, Khmer and Myanmar. Planes 2 and 3 hold only CJK
#: ideographs, so they are declared whole and future extensions land inside.
SCRIPTIO_CONTINUA_BLOCKS: tuple[tuple[int, int, str], ...] = (
    (0x0E00, 0x0E7F, "Thai"),
    (0x0E80, 0x0EFF, "Lao"),
    (0x1000, 0x109F, "Myanmar"),
    (0x1100, 0x11FF, "Hangul Jamo"),
    (0x1780, 0x17FF, "Khmer"),
    (0x19E0, 0x19FF, "Khmer Symbols"),
    (0x2E80, 0x2EFF, "CJK Radicals Supplement"),
    (0x2F00, 0x2FDF, "Kangxi Radicals"),
    (0x3000, 0x303F, "CJK Symbols and Punctuation"),
    (0x3040, 0x309F, "Hiragana"),
    (0x30A0, 0x30FF, "Katakana"),
    (0x3130, 0x318F, "Hangul Compatibility Jamo"),
    (0x31F0, 0x31FF, "Katakana Phonetic Extensions"),
    (0x3400, 0x4DBF, "CJK Unified Ideographs Extension A"),
    (0x4E00, 0x9FFF, "CJK Unified Ideographs"),
    (0xA960, 0xA97F, "Hangul Jamo Extended-A"),
    (0xA9E0, 0xA9FF, "Myanmar Extended-B"),
    (0xAA60, 0xAA7F, "Myanmar Extended-A"),
    (0xAC00, 0xD7AF, "Hangul Syllables"),
    (0xD7B0, 0xD7FF, "Hangul Jamo Extended-B"),
    (0xF900, 0xFAFF, "CJK Compatibility Ideographs"),
    (0xFF65, 0xFF9F, "Halfwidth Katakana"),
    (0xFFA0, 0xFFDC, "Halfwidth Hangul"),
    (0x1AFF0, 0x1AFFF, "Kana Extended-B"),
    (0x1B000, 0x1B0FF, "Kana Supplement"),
    (0x1B100, 0x1B12F, "Kana Extended-A"),
    (0x1B130, 0x1B16F, "Small Kana Extension"),
    (0x20000, 0x3FFFF, "Supplementary and Tertiary Ideographic Planes"),
)

#: Letter blocks of the scripts the tokenizer treats specially: Latin words get
#: an accent-folded index variant; Cyrillic, Greek and Armenian words get their
#: own Snowball stemmer. A word qualifies only when EVERY letter in it falls in
#: one script's blocks; marks and digits do not vote.
LETTER_SCRIPT_BLOCKS: dict[str, tuple[tuple[int, int], ...]] = {
    "latin": (
        (0x0041, 0x005A),
        (0x0061, 0x007A),
        (0x00AA, 0x00AA),
        (0x00BA, 0x00BA),
        (0x00C0, 0x024F),  # Latin-1 letters, Latin Extended-A and -B
        (0x0250, 0x02AF),  # IPA Extensions
        (0x1E00, 0x1EFF),  # Latin Extended Additional
        (0x2C60, 0x2C7F),  # Latin Extended-C
        (0xA720, 0xA7FF),  # Latin Extended-D
        (0xAB30, 0xAB6F),  # Latin Extended-E
        (0xFB00, 0xFB06),  # Latin ligatures
        (0x10780, 0x107BF),  # Latin Extended-F
        (0x1DF00, 0x1DFFF),  # Latin Extended-G
    ),
    "cyrillic": (
        (0x0400, 0x052F),  # Cyrillic and Cyrillic Supplement
        (0x1C80, 0x1C8F),  # Cyrillic Extended-C
        (0x2DE0, 0x2DFF),  # Cyrillic Extended-A
        (0xA640, 0xA69F),  # Cyrillic Extended-B
        (0x1E030, 0x1E08F),  # Cyrillic Extended-D
    ),
    "greek": (
        (0x0370, 0x03FF),  # Greek and Coptic
        (0x1F00, 0x1FFF),  # Greek Extended
    ),
    "armenian": (
        (0x0530, 0x058F),
        (0xFB13, 0xFB17),  # Armenian ligatures
    ),
}

_CONTINUA_STARTS = tuple(start for start, _end, _name in SCRIPTIO_CONTINUA_BLOCKS)
_CONTINUA_ENDS = tuple(end for _start, end, _name in SCRIPTIO_CONTINUA_BLOCKS)


def _within(code_point: int, starts: tuple[int, ...], ends: tuple[int, ...]) -> bool:
    at = bisect.bisect_right(starts, code_point) - 1
    return at >= 0 and code_point <= ends[at]


def is_scriptio_continua(character: str) -> bool:
    """True when `character` belongs to a declared unspaced script's block."""
    return _within(ord(character), _CONTINUA_STARTS, _CONTINUA_ENDS)


#: The Hiragana block. Japanese writes particles, inflections and okurigana
#: in hiragana, so a bigram touching one mostly records grammar.
HIRAGANA_BLOCK = (0x3040, 0x309F)


def is_hiragana(character: str) -> bool:
    """True when `character` is in the declared Hiragana block."""
    return HIRAGANA_BLOCK[0] <= ord(character) <= HIRAGANA_BLOCK[1]


#: Japanese particles and copulas, a closed grammatical set: a hiragana run of
#: a Japanese turn is a word edge only when the WHOLE run is one of these (or a
#: declared filler word), so a name that is itself written partly in hiragana
#: (`ねこやなぎ銀行`) keeps its own edge instead of handing it to its kanji tail.
JAPANESE_PARTICLES = frozenset(
    {
        "の", "を", "に", "は", "が", "で", "と", "へ", "も", "や", "か",
        "から", "まで", "より", "など", "だけ", "しか", "って", "けど", "ので", "のに",
        "では", "には", "とは", "での", "への", "との", "からの", "までの",
        "にも", "でも", "とも", "へは", "のは", "のが", "のを", "のも",
        "について", "として", "にとって", "による", "によって", "のため",
        "だ", "です", "でした", "だった",
    }
)


def vocabulary_words(text: str) -> list[str]:
    """The words of normalised, non-ASCII `text` for vocabulary comparisons.

    A word is a maximal run of letters, digits and combining marks, as
    `[a-z0-9]+` reads ASCII: every other character separates. Where a word
    changes between an unspaced script and any other, it splits: a Latin
    name glued to a Japanese phrase is its own word. Japanese writes its
    particles and inflections in hiragana, so a Japanese run splits at its
    hiragana and keeps only what lies between (`エアコンの故障` holds
    `エアコン` and `故障`); a run of hiragana alone is kept whole.
    """
    words: list[str] = []
    parts: list[list[str]] = []

    def close() -> None:
        only_hiragana = all(kind == "hiragana" for kind, _part in parts)
        words.extend(part for kind, part in parts if kind != "hiragana" or only_hiragana)
        parts.clear()

    for character in text:
        category = unicodedata.category(character)[0]
        if category == "M" and parts:
            parts[-1][1] += character
            continue
        if category not in ("L", "N"):
            close()
            continue
        kind = (
            "hiragana"
            if is_hiragana(character)
            else "continua"
            if is_scriptio_continua(character)
            else "spaced"
        )
        if parts and parts[-1][0] == kind:
            parts[-1][1] += character
        else:
            parts.append([kind, character])
    close()
    return words


#: Unicode planes searched when the character tables are built. Every
#: combining mark, symbol and variation selector in Unicode sits in the Basic
#: or Supplementary Multilingual Plane or in plane 14.
MARK_PLANES = ((0x0000, 0x1FFFF), (0xE0000, 0xEFFFF))

#: Variation selectors choose a glyph (text or emoji presentation, an
#: ideographic variant); they carry no letter, so they are dropped before
#: tokenizing rather than kept as marks that would glue a keycap to its digit.
VARIATION_SELECTORS = ((0x180B, 0x180D), (0x180F, 0x180F), (0xFE00, 0xFE0F), (0xE0100, 0xE01EF))


def in_mark_planes(code_point: int) -> bool:
    return any(start <= code_point <= end for start, end in MARK_PLANES)


@lru_cache(maxsize=1)
def character_tables() -> tuple[str, dict[int, str | None]]:
    """(regex class body of every combining mark, raw-text translation table).

    The table runs before NFKC on non-ASCII text. It maps every non-ASCII
    symbol (S*) and enclosing mark (Me) to a space, so NFKC can never turn one
    into letters that join the word beside it ("Zorblex™" would otherwise
    become `zorblextm`, "20℃" `20c`), and it deletes variation selectors.
    Built once, from the running interpreter's Unicode data.
    """
    ranges: list[list[int]] = []
    table: dict[int, str | None] = {}
    for start, end in MARK_PLANES:
        for code_point in range(start, end + 1):
            category = unicodedata.category(chr(code_point))
            if category[0] == "M":
                if ranges and ranges[-1][1] == code_point - 1:
                    ranges[-1][1] = code_point
                else:
                    ranges.append([code_point, code_point])
                if category == "Me":
                    table[code_point] = " "
            elif category[0] == "S" and code_point > 0x7F:
                table[code_point] = " "
    for low, high in VARIATION_SELECTORS:
        for code_point in range(low, high + 1):
            table[code_point] = None
    marks = "".join(f"\\U{low:08x}-\\U{high:08x}" for low, high in ranges)
    return marks, table


_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")


def comparison_words(text: str) -> list[str]:
    """The unstemmed words of `text`, for matching labels against labels.

    ASCII text reads as `[a-z0-9]+` over its lowercase, exactly as these
    comparisons always have. Other text goes through the raw-text table of
    `character_tables` (symbols separate, so "Zorblex™" stays `zorblex`), NFKC
    and casefolding, then `vocabulary_words`, so a Cyrillic, Greek or Japanese
    label yields its words instead of nothing. Search ranking stems; use
    `bm25.tokenize` there instead.
    """
    if text.isascii():
        return _ASCII_WORD_RE.findall(text.lower())
    table = character_tables()[1]
    return vocabulary_words(unicodedata.normalize("NFKC", text.translate(table)).casefold())


def continua_character_class() -> str:
    """The declared unspaced blocks as the body of a regex character class."""
    return "".join(
        f"\\U{start:08x}-\\U{end:08x}" for start, end, _name in SCRIPTIO_CONTINUA_BLOCKS
    )


_LETTER_SCRIPT_BOUNDS = {
    script: (tuple(start for start, _end in blocks), tuple(end for _start, end in blocks))
    for script, blocks in LETTER_SCRIPT_BLOCKS.items()
}


@lru_cache(maxsize=4096)
def _letter_script(character: str) -> str:
    """The declared script of one letter, or "" when it is in none of them."""
    code_point = ord(character)
    for script, (starts, ends) in _LETTER_SCRIPT_BOUNDS.items():
        if _within(code_point, starts, ends):
            return script
    return ""


def uniform_letter_script(token: str) -> str | None:
    """The one declared script every letter of `token` belongs to, else None.

    Returns None for a token with no letters, with letters of two scripts, or
    with any letter outside the declared scripts.
    """
    script: str | None = None
    for character in token:
        if not unicodedata.category(character).startswith("L"):
            continue
        found = _letter_script(character)
        if not found or (script is not None and found != script):
            return None
        script = found
    return script
