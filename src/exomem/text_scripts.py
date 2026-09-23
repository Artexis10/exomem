"""Declared Unicode script data for lexical tokenization.

One table in one module: which code points belong to a script written without
spaces between words (scriptio continua), and which letters belong to the
scripts the lexical tokenizer stems or accent-folds. It is data about scripts,
declared as Unicode block ranges, never a word list or a language detector.

`bm25.tokenize` reads it to decide where an unspaced run starts and which
Snowball stemmer, if any, a spaced word gets. Any other rule that needs to know
whether text is unspaced (a containment match over a CJK run, a chunk cap for
unspaced paragraphs) should read the same table rather than declare its own.
"""

from __future__ import annotations

import bisect
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
