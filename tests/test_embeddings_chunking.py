"""Chunking for text written without spaces between words.

`chunk_text` caps a paragraph at `MAX_WORDS_PER_CHUNK` whitespace words. An
unspaced paragraph (Japanese, Chinese, Thai, ...) is one "word" to that rule, so
it was never capped and the encoder silently truncated it at its sequence
limit. A paragraph whose token characters are mostly scriptio continua now
splits into pieces of at most `MAX_UNSPACED_CHARS_PER_CHUNK` characters, at
sentence punctuation where it can and hard at the limit where it cannot. Every
other paragraph is chunked exactly as before.
"""

from __future__ import annotations

import random

import pytest

from exomem import embeddings


def _legacy_chunk_text(title: str, body: str) -> list[str]:
    """The pre-change algorithm, kept verbatim as the reference for spaced text."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not body:
        return [title] if title else []
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    out: list[str] = []
    for p in paragraphs:
        words = p.split()
        if len(words) > embeddings.MAX_WORDS_PER_CHUNK:
            p = " ".join(words[: embeddings.MAX_WORDS_PER_CHUNK])
        out.append(f"{title}\n\n{p}" if title else p)
    return out


_SPACED_ALPHABETS = (
    "abcdefghijklmnopqrstuvwxyz",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "äöüõßéèàçñ",
    "абвгдеёжзийклмнопрстуфхцчшщыэюя",
    "αβγδεζηθικλμνξοπρστυφχψω",
    "हिन्दी",
)


def _random_spaced_body(rng: random.Random) -> str:
    paragraphs = []
    for _ in range(rng.randint(1, 4)):
        words = []
        for _ in range(rng.choice((3, 40, 349, 350, 351, 900))):
            alphabet = rng.choice(_SPACED_ALPHABETS)
            words.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 9))))
            if rng.random() < 0.1:
                words[-1] += rng.choice(".!?,;:")
        separator = rng.choice((" ", "  ", "\n", " \t "))
        paragraphs.append(separator.join(words))
    return rng.choice(("\n\n", "\n\n\n", "\n \n\n")).join(paragraphs)


def test_spaced_text_chunks_exactly_as_before() -> None:
    rng = random.Random(20260923)
    for _ in range(400):
        title = rng.choice(("", "Title", "Notes on the pump"))
        body = _random_spaced_body(rng)
        assert embeddings.chunk_text(title, body) == _legacy_chunk_text(title, body)


def test_spaced_paragraph_with_a_few_unspaced_characters_keeps_the_word_cap() -> None:
    body = " ".join(["word"] * 1000) + " 東京"
    assert embeddings.chunk_text("T", body) == _legacy_chunk_text("T", body)


def _japanese_paragraph(sentences: int) -> str:
    sentence = "倉庫の在庫は毎週月曜日に確認され、不足している部品は翌日までに発注される"
    return "".join(f"{sentence}{i % 10}。" for i in range(sentences))


def test_a_long_japanese_paragraph_splits_at_sentence_ends_under_the_cap() -> None:
    paragraph = _japanese_paragraph(40)
    assert len(paragraph) > 3 * embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
    chunks = embeddings.chunk_text("在庫管理", paragraph)
    assert len(chunks) > 1
    pieces = []
    for chunk in chunks:
        title, piece = chunk.split("\n\n", 1)
        assert title == "在庫管理"
        assert 0 < len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
        assert piece.endswith("。")
        pieces.append(piece)
    assert "".join(pieces) == paragraph


def test_an_unspaced_sentence_longer_than_the_cap_is_cut_hard() -> None:
    paragraph = "あ" * (2 * embeddings.MAX_UNSPACED_CHARS_PER_CHUNK + 7)
    chunks = embeddings.chunk_text("", paragraph)
    assert [len(chunk) for chunk in chunks] == [
        embeddings.MAX_UNSPACED_CHARS_PER_CHUNK,
        embeddings.MAX_UNSPACED_CHARS_PER_CHUNK,
        7,
    ]
    assert "".join(chunks) == paragraph


def test_a_short_unspaced_paragraph_stays_one_chunk() -> None:
    paragraph = _japanese_paragraph(3)
    assert len(paragraph) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
    assert embeddings.chunk_text("T", paragraph) == [f"T\n\n{paragraph}"]


def test_full_width_and_ascii_sentence_marks_both_end_a_piece() -> None:
    first = "長" * 300 + "！"
    second = "文" * 300 + "?"
    third = "字" * 100
    chunks = embeddings.chunk_text("", first + second + third)
    assert chunks == [first, second + third]


def test_korean_paragraph_is_capped_even_though_it_is_spaced() -> None:
    # Hangul words are separated by spaces but each word is several subword
    # tokens, so the 350-word cap alone would overrun the encoder's limit.
    paragraph = " ".join(["창고의 재고는 매주 월요일에 확인됩니다."] * 60)
    chunks = embeddings.chunk_text("", paragraph)
    assert len(chunks) > 1
    assert all(len(chunk) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK for chunk in chunks)
    assert " ".join(chunks) == paragraph


def test_unspaced_and_spaced_paragraphs_on_one_page_each_follow_their_rule() -> None:
    english = " ".join(["word"] * 400)
    japanese = _japanese_paragraph(40)
    chunks = embeddings.chunk_text("T", f"{english}\n\n{japanese}")
    assert chunks[0] == _legacy_chunk_text("T", english)[0]
    assert len(chunks) > 2
    assert all(
        len(chunk.split("\n\n", 1)[1]) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
        for chunk in chunks[1:]
    )


def _pieces(chunks: list[str], title: str) -> list[str]:
    return [chunk.split("\n\n", 1)[1] if title else chunk for chunk in chunks]


def test_a_paragraph_with_a_word_longer_than_the_cap_is_split_by_characters() -> None:
    # A spaced paragraph whose one "word" (a pasted blob, a path chain) is longer
    # than the cap: the word cap would keep it whole.
    blob = "".join(chr(ord("a") + (index * 7) % 26) for index in range(1300))
    paragraph = f"The payload was {blob} and it failed the check."
    chunks = embeddings.chunk_text("T", paragraph)
    pieces = _pieces(chunks, "T")
    assert len(pieces) > 2
    assert all(len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK for piece in pieces)
    assert "".join(pieces).replace(" ", "") == paragraph.replace(" ", "")


def test_unspaced_text_mixed_with_ascii_identifiers_is_capped() -> None:
    # No spaces, and fewer than half of the token characters are unspaced.
    paragraph = (
        "設定ファイルのパスは/etc/example/config.jsonで、キーはEXAMPLE_VAULT_PATHと"
        "EXAMPLE_STATE_ROOTである。"
    ) * 15
    pieces = embeddings.chunk_text("", paragraph)
    assert len(pieces) > 1
    assert all(len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK for piece in pieces)
    assert "".join(pieces) == paragraph


def test_a_spaced_paragraph_carrying_long_unspaced_text_is_capped() -> None:
    # Just under half unspaced, with spaces between runs: the word cap alone
    # would keep about 5,000 characters.
    paragraph = " ".join(f"東京都港区芝公園四丁目 config_value_{index}" for index in range(200))
    pieces = embeddings.chunk_text("", paragraph)
    assert len(pieces) > 1
    assert all(len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK for piece in pieces)


def test_a_hard_cut_backs_up_to_the_last_space() -> None:
    # Thai separates phrases with spaces and has no sentence marks.
    phrase = "ระบบจะบันทึกการเปลี่ยนแปลงลงในบันทึกล่วงหน้าก่อนแก้ไขหน้าข้อมูล"
    paragraph = " ".join([phrase] * 40)
    pieces = embeddings.chunk_text("", paragraph)
    assert len(pieces) > 1
    for piece in pieces:
        assert len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
        assert set(piece.split(" ")) == {phrase}


def test_a_hard_cut_never_starts_a_piece_with_a_combining_mark() -> None:
    import unicodedata

    # A base letter and its tone mark, repeated with no space to back up to,
    # offset by one letter so that the cap falls between a letter and its mark.
    paragraph = "ก" + "ก่" * 400
    pieces = embeddings.chunk_text("", paragraph)
    assert len(pieces) > 1
    assert "".join(pieces) == paragraph
    for piece in pieces:
        assert len(piece) <= embeddings.MAX_UNSPACED_CHARS_PER_CHUNK
        assert not unicodedata.category(piece[0]).startswith("M")


_ADVERSARIAL = {
    "ja prose": ("在庫管理", "倉庫の在庫は毎週月曜日に確認され、不足している部品は翌日までに発注される。" * 40),
    "zh prose": ("预写日志", "数据库在修改数据页之前先把变更写入预写日志，这样崩溃之后可以重放日志恢复一致状态。" * 40),
    "th prose": ("บันทึก", "ระบบจะบันทึกการเปลี่ยนแปลงลงในบันทึกล่วงหน้าก่อนแก้ไขหน้าข้อมูล " * 40),
    "ko prose": ("재고", "창고의 재고는 매주 월요일에 확인됩니다. " * 60),
    "ja with identifiers": (
        "設定",
        "設定ファイルのパスは/etc/example/config.jsonで、キーはEXAMPLE_VAULT_PATHと"
        "EXAMPLE_STATE_ROOTである。" * 15,
    ),
    "spaced half-unspaced": (
        "",
        " ".join(f"東京都港区芝公園四丁目 config_value_{index}" for index in range(200)),
    ),
    "ascii blob": ("Blob", "payload " + "".join(chr(ord("a") + (i * 7) % 26) for i in range(3000))),
    "digits blob": ("", "".join(str(index % 10) for index in range(3000))),
}


@pytest.mark.embeddings
@pytest.mark.parametrize("case", sorted(_ADVERSARIAL))
def test_no_character_capped_chunk_exceeds_the_encoder_limit(case: str) -> None:
    tokenizers = pytest.importorskip("tokenizers")
    huggingface_hub = pytest.importorskip("huggingface_hub")
    path = huggingface_hub.try_to_load_from_cache("BAAI/bge-m3", "tokenizer.json")
    if not isinstance(path, str):
        pytest.skip("the bge-m3 tokenizer is not in the local model cache")
    tokenizer = tokenizers.Tokenizer.from_file(path)
    title, body = _ADVERSARIAL[case]
    for chunk in embeddings.chunk_text(title, body):
        # encode() adds the two special tokens the encoder also counts.
        assert len(tokenizer.encode(chunk).ids) <= 512, (case, len(chunk))
