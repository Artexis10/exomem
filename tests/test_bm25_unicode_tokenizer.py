"""Tokenizer v2: Unicode lexical tokens that stay byte-identical on ASCII.

The lexical catalogue, the in-process BM25 rung, find's stem gates and
activation's lexical evidence all read `bm25.tokenize`. Version 2 widens it from
`[a-z0-9]+` to every script, under one hard rule: an ASCII text yields exactly
the tokens it always did, in index mode and in query mode, so an English vault's
index and scores do not move. The legacy tokenizer is frozen below as the oracle.
"""

from __future__ import annotations

import random
import re
import string
import sys
import unicodedata
from pathlib import Path

import pytest
import snowballstemmer

from exomem import bm25, text_scripts

_LEGACY_TOKEN = re.compile(r"[a-z0-9]+")
_LEGACY_STEMMER = snowballstemmer.stemmer("english")
_FIXTURES = Path(__file__).resolve().parent / "fixtures"

_WORD_BANK = (
    "regulation", "regulator", "compounding", "running", "caches", "o'brien",
    "girvan-slot", "state-of-the-art", "measure?", "e.g.", "C++", "x86_64",
    "HTTP/2", "v1.2.3", "don't", "rock'n'roll", "snake_case_name", "CamelCase",
    "2026-09-23", "ABC123def", "the", "and", "is", "flying", "harbour", "ledger",
)


def _legacy_tokenize(text: str) -> list[str]:
    """Tokenizer v1, frozen: lowercase, `[a-z0-9]+`, English Snowball."""
    return [_LEGACY_STEMMER.stemWord(word) for word in _LEGACY_TOKEN.findall(text.lower())]


def _random_ascii(rng: random.Random) -> str:
    shape = rng.random()
    if shape < 0.4:
        alphabet = string.printable
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
    if shape < 0.5:
        return "".join(chr(rng.randint(0, 127)) for _ in range(rng.randint(0, 40)))
    separators = (" ", "  ", "\n", "\t", ", ", ". ", "-", "_", "'", "/", "(", ")", ":", "")
    words = [rng.choice(_WORD_BANK) for _ in range(rng.randint(0, 12))]
    return "".join(word + rng.choice(separators) for word in words)


# --------------------------------------------------------------- ASCII identity


def test_ascii_index_and_query_tokens_equal_the_legacy_tokenizer() -> None:
    rng = random.Random(20260923)
    for _ in range(20_000):
        text = _random_ascii(rng)
        expected = _legacy_tokenize(text)
        assert bm25.tokenize(text) == expected, text
        assert bm25.tokenize(text, query=True) == expected, text


def test_ascii_units_are_single_legacy_stems_in_both_modes() -> None:
    rng = random.Random(7)
    for _ in range(2_000):
        text = _random_ascii(rng)
        expected = [(stem,) for stem in _legacy_tokenize(text)]
        for query in (False, True):
            units = bm25.token_units(text, query=query)
            assert [unit.stems for unit in units] == expected, text
            assert not any(unit.run for unit in units)


def test_ascii_with_one_non_ascii_separator_takes_the_scanner_and_stays_identical() -> None:
    """The fast-path/scanner boundary: one non-ASCII character sends the whole
    text through the Unicode scanner, which must reproduce the ASCII tokens."""
    rng = random.Random(11)
    separators = ("—", "…", "→", "·", " ", "’", "─", "﻿")
    for _ in range(3_000):
        text = _random_ascii(rng)
        at = rng.randint(0, len(text))
        mixed = text[:at] + rng.choice(separators) + text[at:]
        assert not mixed.isascii()
        expected = _legacy_tokenize(mixed)
        assert bm25.tokenize(mixed) == expected, mixed
        assert bm25.tokenize(mixed, query=True) == expected, mixed


def test_every_golden_fixture_page_tokenizes_exactly_as_before() -> None:
    """The retrieval golden fixture is English with typographic punctuation. Its
    lexical index, and so the golden gate's lexical lane, is byte-identical."""
    pages = sorted(_FIXTURES.rglob("*.md"))
    assert pages
    non_ascii = 0
    for page in pages:
        text = page.read_text(encoding="utf-8")
        non_ascii += not text.isascii()
        assert bm25.tokenize(text) == _legacy_tokenize(text), page
        assert bm25.tokenize(text, query=True) == _legacy_tokenize(text), page
    assert non_ascii, "the fixture must exercise the scanner, not only the fast path"


def test_stem_word_memo_keeps_english_snowball_for_ascii() -> None:
    words = [word for word in _WORD_BANK] + ["regulations", "generously", "caresses"]
    bm25.stem_word.cache_clear()
    try:
        first = [bm25.stem_word(word) for word in words]
        # A non-ASCII lookup between the two passes must not disturb the memo.
        bm25.stem_word("книгами")
        second = [bm25.stem_word(word) for word in words]
    finally:
        bm25.stem_word.cache_clear()
    expected = [_LEGACY_STEMMER.stemWord(word) for word in words]
    assert first == expected
    assert second == expected


# --------------------------------------------------------------- normalisation


def test_nfkc_folds_ligatures_and_full_width_forms_before_splitting() -> None:
    assert bm25.tokenize("ﬁnal") == ["final"]
    assert bm25.tokenize("Ｒｕｎｎｉｎｇ") == ["run"]
    assert bm25.tokenize("ＡＢＣ１２３") == ["abc123"]
    assert bm25.tokenize("２０２６年") == ["2026", "年"]


def test_casefold_applies_beyond_ascii() -> None:
    assert bm25.tokenize("STRASSE Straße", query=True) == ["strass", "strass"]
    assert bm25.tokenize("КНИГИ", query=True) == ["книг"]


# --------------------------------------------------------------- unspaced scripts


def test_unspaced_runs_emit_overlapping_bigrams() -> None:
    assert bm25.tokenize("東京タワー") == [
        "東京", "京タ", "タワ", "ワー",
    ]
    assert bm25.tokenize("日本語") == ["日本", "本語"]
    assert bm25.tokenize("한국어") == ["한국", "국어"]


def test_a_single_character_run_emits_that_character() -> None:
    assert bm25.tokenize("日") == ["日"]
    assert bm25.tokenize("日 月") == ["日", "月"]


def test_runs_split_where_the_script_class_changes() -> None:
    assert bm25.tokenize("Python入門", query=True) == ["python", "入門"]
    assert bm25.tokenize("2026年の計画", query=True) == [
        "2026", "年の", "の計", "計画",
    ]


def test_thai_marks_stay_on_their_base_character() -> None:
    # "กิน" is ko kai + sara i (a combining mark) + no nu: two
    # characters, one bigram. The mark never becomes a token of its own.
    assert bm25.tokenize("กิน") == ["กิน"]
    tokens = bm25.tokenize("ภาษาไทย")
    assert tokens == [
        "ภา", "าษ", "ษา", "าไ", "ไท", "ทย",
    ]


def test_devanagari_words_are_kept_whole_with_their_marks() -> None:
    word = "हिन्दी"
    assert bm25.tokenize(word) == [word]
    assert bm25.tokenize(word, query=True) == [word]


# --------------------------------------------------------------- stemming by script


def test_cyrillic_uses_russian_snowball() -> None:
    forms = ("книгами", "книги", "книга")
    assert {bm25.stem_word(form) for form in forms} == {"книг"}
    assert bm25.tokenize("Поставкой", query=True) == [
        "поставк"
    ]


def test_greek_uses_greek_snowball_after_casefold() -> None:
    # casefold turns the final sigma into a medial one; the stemmer agrees.
    assert bm25.tokenize("Λόγος λόγου", query=True) == [
        "λογ", "λογ",
    ]


def test_armenian_uses_armenian_snowball() -> None:
    assert bm25.stem_word("քաղաքներ") == bm25.stem_word(
        "քաղաքը"
    )


def test_other_spaced_scripts_and_non_ascii_latin_are_not_stemmed() -> None:
    assert bm25.stem_word("zölvarnud") == "zölvarnud"
    assert bm25.stem_word("हिन्दी") == "हिन्दी"
    # A token mixing two scripts' letters is left alone rather than guessed at.
    assert bm25.stem_word("книгami") == "книгami"


# --------------------------------------------------------------- Latin fold


def test_index_side_adds_an_accent_folded_variant_for_latin_words() -> None:
    assert bm25.tokenize("Zölvarn") == ["zölvarn", "zolvarn"]
    assert bm25.tokenize("résumé") == ["résumé", "resum"]
    assert bm25.tokenize("İstanbul") == ["i̇stanbul", "istanbul"]


def test_query_side_emits_the_surface_form_only() -> None:
    assert bm25.tokenize("Zölvarn", query=True) == ["zölvarn"]
    assert bm25.tokenize("zolvarn", query=True) == ["zolvarn"]
    assert bm25.tokenize("résumé", query=True) == ["résumé"]


def test_the_fold_is_restricted_to_latin_words() -> None:
    # Dropping marks would destroy an Indic word and conflate Cyrillic й with и.
    word = "हिन्दी"
    assert bm25.tokenize(word) == [word]
    assert bm25.tokenize("йод") == [bm25.stem_word("йод")]
    assert "иод" not in bm25.tokenize("йод")


def test_a_word_with_no_marks_gets_no_variant() -> None:
    assert bm25.tokenize("søren") == ["søren"]
    assert bm25.tokenize("æble") == ["æble"]


# --------------------------------------------------------------- units


def test_token_units_group_stems_by_word_and_run() -> None:
    text = "Zölvarn and 東京タワー"
    index_units = bm25.token_units(text)
    assert [unit.stems for unit in index_units] == [
        ("zölvarn", "zolvarn"),
        ("and",),
        ("東京", "京タ", "タワ", "ワー"),
    ]
    assert [unit.run for unit in index_units] == [False, False, True]
    query_units = bm25.token_units(text, query=True)
    assert [unit.stems for unit in query_units] == [
        ("zölvarn",),
        ("and",),
        ("東京", "京タ", "タワ", "ワー"),
    ]


def test_tokenize_is_the_flattened_units() -> None:
    rng = random.Random(3)
    samples = [
        "Zölvarn and 東京タワー",
        "Поставка — résumé, girvan-slot",
        "ภาษาไทย हिन्दी 한국어",
    ] + [_random_ascii(rng) for _ in range(200)]
    for text in samples:
        for query in (False, True):
            flat = [stem for unit in bm25.token_units(text, query=query) for stem in unit.stems]
            assert bm25.tokenize(text, query=query) == flat, text


def test_a_word_is_present_when_any_form_occurs_and_a_run_when_most_bigrams_do() -> None:
    word = bm25.token_units("Zölvarn")[0]
    assert bm25.unit_present(word, {"zolvarn"})
    assert not bm25.unit_present(word, {"other"})
    run = bm25.token_units("会議の議事録", query=True)[0]
    assert run.run and len(set(run.stems)) == 5
    # 会議 + 議事 + 事録 of 会議の議事録: three of five bigrams is a strict majority.
    assert bm25.unit_present(run, {"会議", "議事", "事録"})
    assert not bm25.unit_present(run, {"会議", "議事"})


def test_no_token_ever_contains_a_quote_or_whitespace() -> None:
    rng = random.Random(5)
    for _ in range(2_000):
        text = "".join(chr(rng.randint(0x20, 0x3100)) for _ in range(rng.randint(1, 30)))
        for token in bm25.tokenize(text):
            assert token
            assert '"' not in token
            assert not any(character.isspace() for character in token)


# --------------------------------------------------------------- the script table


@pytest.mark.parametrize(
    "character",
    ["日", "あ", "ア", "ー", "々", "한", "ก", "ກ", "ក", "က", "\U00020000"],
)
def test_unspaced_scripts_are_declared_scriptio_continua(character: str) -> None:
    assert text_scripts.is_scriptio_continua(character)


@pytest.mark.parametrize("character", ["a", "ä", "я", "α", "ա", "ह", "1", " ", "—"])
def test_spaced_scripts_are_not_scriptio_continua(character: str) -> None:
    assert not text_scripts.is_scriptio_continua(character)


def test_uniform_letter_script_names_the_stemmed_and_folded_scripts() -> None:
    assert text_scripts.uniform_letter_script("zölvarn2") == "latin"
    assert text_scripts.uniform_letter_script("книга") == "cyrillic"
    assert text_scripts.uniform_letter_script("λογοσ") == "greek"
    assert text_scripts.uniform_letter_script("քաղաք") == "armenian"
    assert text_scripts.uniform_letter_script("книгami") is None
    assert text_scripts.uniform_letter_script("हिन्दी") is None
    assert text_scripts.uniform_letter_script("2026") is None


def test_the_token_character_class_is_letters_numbers_and_marks_on_this_interpreter() -> None:
    """The scanner's regex class must be exactly Unicode L*, N* and M*: FTS5's
    unicode61 is declared with those categories, and a character one side treats
    as a separator and the other as a letter would split a token."""
    word = re.compile(r"[^\W_]")
    marks_outside_scanned_planes = []
    for code_point in range(sys.maxunicode + 1):
        character = chr(code_point)
        category = unicodedata.category(character)
        assert bool(word.match(character)) == (category[0] in "LN"), hex(code_point)
        if category[0] == "M" and not bm25._in_mark_planes(code_point):
            marks_outside_scanned_planes.append(hex(code_point))
    assert marks_outside_scanned_planes == []
    for code_point in (0x0301, 0x093F, 0x0E34, 0x3099, 0xFE0F, 0xE0100):
        assert bm25._is_token_character(chr(code_point))


def test_tokenizer_version_is_two() -> None:
    assert bm25.TOKENIZER_VERSION == 2
