"""The compact Unigram tokenizer must give HF's ids, or the encoder must keep HF."""

from __future__ import annotations

import json
import logging
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest

tokenizers = pytest.importorskip("tokenizers")

from exomem import compact_tokenizer, embedding_backend  # noqa: E402

MAX_LENGTH = 8
PAD_TOKEN = "<pad>"

# Score literals are spliced into the JSON as text so that HF's own parser reads
# them. -7.4715776443481445 is one of bge-m3's: serde_json reads it as
# -7.471577644348144, Python as -7.4715776443481445. "pqr" then ties exactly
# between p+qr and pq+r under HF's reading and not under Python's, which is what
# tells a tokenizer that took its scores from Python from one that took HF's.
_VOCAB = [
    ("<s>", "0.0"), ("<pad>", "0.0"), ("</s>", "0.0"), ("<unk>", "0.0"),
    ("▁", "0.0"), ("▁a", "-4.0"), ("a", "-6.0"), ("b", "-6.5"), ("ab", "-9.0"),
    # c+cc and cc+c sum to the same f64, so which one wins is the tie rule alone.
    ("c", "-3.0"), ("cc", "-5.0"),
    ("p", "-7.4715776443481445"), ("q", "-20.0"), ("r", "-0.5"), ("qr", "-0.25"), ("pq", "-7.221577644348144"),
    ("é", "-5.0"), ("東", "-6.0"), ("京", "-6.0"), ("東京", "-8.0"),
    ("z", "-30.0"),  # the lowest score, which prices an unknown character
    ("<mask>", "0.0"),
]
_SPECIALS = {"<s>": 0, "<pad>": 1, "</s>": 2, "<unk>": 3, "<mask>": len(_VOCAB) - 1}

_TEXTS = [
    "",
    "   ",
    "ab a b",
    "ccc cccc ^obs-ccc85a8aac24",
    "pqr pq pqr",
    "東京 東 京 東京東京",
    "é é",  # NFKC composes the second
    "FULL ＦＵＬＬ ① ligature ﬁ",
    "emoji 👩‍💻🏳️‍🌈 ☃☃☃ ☃",
    "<s> literal </s> <pad> <unk> specials",
    "a <mask> b",
    "a\u001f<mask>\u001f b",
    "x<mask>y<mask><mask>",
    "a" * 300,
    "ab " * 40,
    "c" * 100 + " z " + "p" * 30,
    "a\tb\nc  d",
    "control\x01\x02\x7f chars",
]


def _tokenizer_json(vocab=_VOCAB, *, normalized: bool = False) -> str:
    def special(content: str) -> dict:
        return {
            "id": _SPECIALS[content], "content": content, "single_word": False, "lstrip": content == "<mask>",
            "rstrip": False, "normalized": normalized and content == "<mask>", "special": True,
        }

    def token(content: str) -> dict:
        return {"SpecialToken": {"id": content, "type_id": 0}}

    template = [token("<s>"), {"Sequence": {"id": "A", "type_id": 0}}, token("</s>")]
    marks = {c: {"id": c, "ids": [_SPECIALS[c]], "tokens": [c]} for c in ("<s>", "</s>")}
    spec = {
        "version": "1.0", "truncation": None, "padding": None, "decoder": None,
        "added_tokens": [special(content) for content in _SPECIALS],
        "normalizer": {"type": "NFKC"},
        "pre_tokenizer": {"type": "Metaspace", "replacement": "▁", "prepend_scheme": "always", "split": True},
        "post_processor": {"type": "TemplateProcessing", "single": template, "pair": template, "special_tokens": marks},
        "model": {
            "type": "Unigram", "unk_id": 3, "byte_fallback": False,
            "vocab": [[piece, f"@@{score}"] for piece, score in vocab],
        },
    }
    # The score literals go in as the text written above, not as Python floats.
    return re.sub(r'"@@([^"]+)"', r"\1", json.dumps(spec))


def _hf(text: str | None = None):
    tokenizer = tokenizers.Tokenizer.from_str(text or _tokenizer_json())
    tokenizer.enable_truncation(max_length=MAX_LENGTH)
    tokenizer.enable_padding(pad_id=_SPECIALS[PAD_TOKEN], pad_token=PAD_TOKEN)
    return tokenizer


def _rows(encodings) -> list[tuple]:
    return [
        (e.ids, e.attention_mask, e.type_ids, e.special_tokens_mask, bool(e.overflowing)) for e in encodings
    ]


def test_compact_tokenizer_gives_the_ids_attention_and_overflow_of_hf() -> None:
    """A vector from the compact tokenizer must be the vector HF's ids would give.

    Only parity over ties, serde-divergent scores, literal specials, lstrip,
    unknown fusing, truncation and padding catches a port that drifts: every one
    of them still yields plausible ids.
    """
    hf = _hf()
    compact = compact_tokenizer._build(hf, MAX_LENGTH, _SPECIALS[PAD_TOKEN])
    assert compact is not None

    for size in (1, 3, len(_TEXTS)):
        for start in range(0, len(_TEXTS), size):
            batch = _TEXTS[start : start + size]
            assert _rows(compact.encode_batch(batch)) == _rows(hf.encode_batch(batch)), batch

    flags = {bool(e.overflowing) for e in hf.encode_batch(_TEXTS)}
    assert flags == {True, False}, "the texts must exercise truncation and its absence"
    assert compact.num_special_tokens_to_add(False) == hf.num_special_tokens_to_add(False)
    assert compact.token_to_id("<mask>") == hf.token_to_id("<mask>")
    assert compact.token_to_id("東京") == hf.token_to_id("東京")
    assert compact.token_to_id("nowhere") is None


def test_a_tokenizer_that_is_not_the_verified_shape_stays_on_hf() -> None:
    """The port is only known to be exact for plain Unigram; anything else must keep HF."""
    wordpiece = tokenizers.Tokenizer(tokenizers.models.WordPiece({"[UNK]": 0, "a": 1}, unk_token="[UNK]"))
    assert compact_tokenizer.from_hf(wordpiece, max_length=MAX_LENGTH, pad_id=0) is None
    # An added token that is normalised is split after the normaliser, which is not ported.
    normalized = _hf(_tokenizer_json(normalized=True))
    assert compact_tokenizer.from_hf(normalized, max_length=MAX_LENGTH, pad_id=1) is None


def test_a_unigram_tokenizer_whose_metaspace_does_not_prepend_to_every_piece_stays_on_hf() -> None:
    """With "first", text around a special token is prefixed differently by HF than piece by piece.

    The specials are renamed so the check cannot lean on XLM-R's own spellings.
    """
    spec = _tokenizer_json().replace('"always"', '"first"').replace("<mask>", "[MASK]")
    hf = _hf(spec)
    assert hf.encode("a[MASK]b").ids != hf.encode("a [MASK] b").ids  # position in the text matters to HF
    assert compact_tokenizer.from_hf(hf, max_length=MAX_LENGTH, pad_id=_SPECIALS[PAD_TOKEN]) is None


def test_a_tokenizer_that_disagrees_with_hf_is_dropped_and_the_drop_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If a future `tokenizers` changes Unigram semantics, vectors must not silently drift."""
    real_build = compact_tokenizer._build

    def drifted(*args):
        compact = real_build(*args)
        compact._pieces.pop("▁")  # every text then tokenises differently
        return compact

    monkeypatch.setattr(compact_tokenizer, "_build", drifted)
    hf = _hf()
    with caplog.at_level(logging.WARNING, logger=compact_tokenizer.__name__):
        assert compact_tokenizer.from_hf(hf, max_length=MAX_LENGTH, pad_id=_SPECIALS[PAD_TOKEN]) is None
    records = [r for r in caplog.records if getattr(r, "event", None) == "compact_tokenizer_fallback"]
    assert [r.fields for r in records] == [{"reason": "parity"}]


class _Session:
    """Stands in for the ONNX Runtime session the encoder loads."""

    def get_inputs(self):
        return [types.SimpleNamespace(name=n) for n in ("input_ids", "attention_mask", "token_type_ids")]

    def run(self, _outputs, feed):
        return [np.ones((*feed["input_ids"].shape, 4), dtype=np.float32)]


def _onnx_encoder(monkeypatch: pytest.MonkeyPatch, path: Path, *, compact: bool):
    session = _Session()
    ort = types.SimpleNamespace(
        SessionOptions=lambda: types.SimpleNamespace(),
        GraphOptimizationLevel=types.SimpleNamespace(ORT_ENABLE_ALL="all"),
        InferenceSession=lambda *_args, **_kwargs: session,
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    profile = embedding_backend.EncoderProfile(
        model="model", pooling="cls", query_prefix="", passage_prefix="",
        max_seq=MAX_LENGTH, pad_token=PAD_TOKEN, collapse_whitespace=True,
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    monkeypatch.setattr(embedding_backend, "require_tokenizer", lambda _name: str(path))
    monkeypatch.setattr(embedding_backend, "read_profile", lambda _name: profile)
    monkeypatch.setattr(embedding_backend, "served_artifact", lambda _name: None)
    monkeypatch.setattr(embedding_backend, "_model_file", lambda *_args: "hub.onnx")
    if not compact:
        monkeypatch.setattr(compact_tokenizer, "from_hf", lambda *_args, **_kwargs: None)
    return embedding_backend._OnnxEncoder("model", "cpu"), session


def test_the_onnx_encoder_selects_the_compact_tokenizer_and_keeps_hf_when_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tokenizer.json"
    path.write_text(_tokenizer_json(), encoding="utf-8")
    with_compact, _ = _onnx_encoder(monkeypatch, path, compact=True)
    assert isinstance(with_compact._tokenizer, compact_tokenizer.CompactUnigramTokenizer)
    with_hf, _ = _onnx_encoder(monkeypatch, path, compact=False)
    assert not isinstance(with_hf._tokenizer, compact_tokenizer.CompactUnigramTokenizer)
