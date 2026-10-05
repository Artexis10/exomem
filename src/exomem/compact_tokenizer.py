"""A compact stand-in for a Unigram `tokenizers.Tokenizer`, with identical token ids.

Why this exists: the Rust Unigram model of a 250k-piece vocabulary (bge-m3)
holds ~264 MiB of private memory per process, the largest single allocation in
an idle hosted cell. The same vocabulary as a Python dict plus an f64 array is
~64 MiB. Only the *model* is replaced: HF's own normalizer and pre-tokenizer
objects stay, so the text that reaches the Viterbi pass is theirs, and the pass
is a port of the Rust `Unigram::encode_optimized`, ties and unknown-piece
fusing included. It is slower per text (0.30 against 0.16 ms), which is noise
next to the encoder run it feeds.

Two details make the ids exact, and both were found by measuring against HF:

* Scores come from HF's own parse (`to_str()`), never from Python's reading of
  `tokenizer.json`: serde_json rounds some literals one ulp away from Python's
  (65,856 of bge-m3's 250,002), and an exact f64 tie then breaks the other way.
* A candidate replaces a node only when it is strictly better (`>=` gave 843
  mismatches on a 117k-text vault corpus).

`from_hf` returns None, and the caller keeps HF, unless the tokenizer is the
plain shape this was verified on and a self-check against HF agrees.
"""

from __future__ import annotations

import array
import functools
import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .log_events import log_event

log = logging.getLogger(__name__)

#: Words are cached per tokenizer: repeated words dominate prose, and a word
#: costs a Viterbi pass. Long words (hashes, URLs) are not worth an entry, and
#: bounding the key keeps the cache's own memory bounded.
_WORD_CACHE_ENTRIES = 8192
_CACHED_WORD_CHARS = 64
#: `Unigram` scores a character no piece covers at the vocabulary's lowest score
#: minus this penalty (`K_UNK_PENALTY`).
_UNK_PENALTY = 10.0
#: Unicode White_Space, which is what the tokenizers crate strips for an added
#: token's `lstrip`/`rstrip`. `str.strip()` also takes U+001C..U+001F.
_WHITESPACE = (
    "\t\n\x0b\x0c\r \x85\xa0 "
    + "".join(chr(code) for code in range(0x2000, 0x200B))
    + "    　"
)

#: Text the build-time check encodes with both tokenizers. It covers what
#: differs between Unigram implementations: scripts without spaces, ZWJ
#: sequences, literal special tokens in the text, repeated characters (equal
#: sums, so tie-breaking), control characters, a very long word and a text long
#: enough to be truncated (appended in `_probes`).
_PROBES = (
    "Café naïve résumé coöperate",
    "東京都の天気は晴れです。今日は良い日",
    "한국어 문장입니다",
    "مرحبا بالعالم",
    "Привет, мир! Ёлка",
    "ภาษาไทยไม่มีช่องว่าง",
    "emoji 👩‍💻🏳️‍🌈 👍🏽 test",
    "ﬁ ligature ＦＵＬＬ ①②",
    "<s> </s> <pad> <unk> <mask> literal specials",
    "x<mask>y <s>z",
    "a <mask> b",
    "a\u001f<mask>\u001f b",
    "control\x01\x02\x7f chars",
    "^obs-ccc85a8aac24",
    "547-12fff863",
    "s-q-epd68937d454e8-20260930t065555568833",
    "x" * 40 + "y" * 40,
    "tab\tnew\nline  two  spaces",
    " leading and trailing ",
    "   ",
    "",
    "a" * 3000,
)


@dataclass(frozen=True, slots=True)
class CompactEncoding:
    """One text's row: the fields of an HF `Encoding` that the encoder reads."""

    ids: list[int]
    attention_mask: list[int]
    type_ids: list[int]
    special_tokens_mask: list[int]
    #: Truthy iff the text was truncated (HF holds the cut tokens here).
    overflowing: bool


class CompactUnigramTokenizer:
    """Encodes like a Unigram `Tokenizer` with right truncation and batch padding."""

    def __init__(
        self,
        *,
        pieces: dict[str, int],
        scores: array.array,
        unk_id: int,
        normalizer,
        pre_tokenizer,
        added: Sequence[tuple[str, int, bool, bool]],
        prefix: Sequence[int],
        suffix: Sequence[int],
        max_length: int,
        pad_id: int,
    ) -> None:
        self._pieces = pieces
        self._scores = scores
        self._unk_id = unk_id
        self._unk_score = min(scores) - _UNK_PENALTY
        self._max_piece = max((len(piece) for piece in pieces), default=1)
        self._normalizer = normalizer
        self._pre_tokenizer = pre_tokenizer
        self._added = {content: (token_id, lstrip, rstrip) for content, token_id, lstrip, rstrip in added}
        # Longest first, so the alternation is leftmost-longest like the crate's matcher.
        self._added_re = (
            re.compile("|".join(re.escape(c) for c in sorted(self._added, key=len, reverse=True)))
            if self._added
            else None
        )
        self._prefix = list(prefix)
        self._suffix = list(suffix)
        self._max_length = max_length
        self._pad_id = pad_id
        self._cached_word = functools.lru_cache(maxsize=_WORD_CACHE_ENTRIES)(self._word_ids)

    def num_special_tokens_to_add(self, is_pair: bool) -> int:
        # Only the single-sequence template is ever built (see `from_hf`).
        return len(self._prefix) + len(self._suffix)

    def token_to_id(self, token: str) -> int | None:
        added = self._added.get(token)
        return added[0] if added else self._pieces.get(token)

    def encode_batch(self, texts: Sequence[str]) -> list[CompactEncoding]:
        budget = self._max_length - self.num_special_tokens_to_add(False)
        rows = [self._content(text, budget) for text in texts]
        width = max((len(content) for content, _ in rows), default=0) + self.num_special_tokens_to_add(False)
        encodings = []
        for content, truncated in rows:
            real = len(self._prefix) + len(content) + len(self._suffix)
            padding = width - real
            encodings.append(
                CompactEncoding(
                    ids=[*self._prefix, *content, *self._suffix, *([self._pad_id] * padding)],
                    attention_mask=[1] * real + [0] * padding,
                    type_ids=[0] * width,
                    # Added tokens inside the text are not special to the mask: HF flags
                    # only the template's own tokens and the padding.
                    special_tokens_mask=[1] * len(self._prefix)
                    + [0] * len(content)
                    + [1] * (len(self._suffix) + padding),
                    overflowing=truncated,
                )
            )
        return encodings

    def _content(self, text: str, budget: int) -> tuple[list[int], bool]:
        """The text's ids cut at `budget`, and whether it ran past it.

        Stops reading once a row is known to overflow: every word is tokenised
        on its own, so the ids kept are the ones the whole text would give.
        """
        out: list[int] = []
        for ids in self._token_groups(text):
            out.extend(ids)
            if len(out) > budget:
                return out[:budget], True
        return out, False

    def _token_groups(self, text: str) -> Iterator[Sequence[int]]:
        """Ids in order: an added token by itself, a word at a time otherwise."""
        for segment, token in self._split_added(text):
            if token is not None:
                yield (token,)
                continue
            normalized = self._normalizer.normalize_str(segment) if self._normalizer else segment
            if self._pre_tokenizer is None:
                words = [normalized]
            else:
                words = [word for word, _ in self._pre_tokenizer.pre_tokenize_str(normalized)]
            for word in words:
                yield self._cached_word(word) if len(word) <= _CACHED_WORD_CHARS else self._word_ids(word)

    def _split_added(self, text: str) -> Iterator[tuple[str, int | None]]:
        """The text cut around its added tokens, as `AddedVocabulary` does.

        Yields (token_text, None) for text between tokens and ("", id) for a
        token; `lstrip`/`rstrip` consume the whitespace around it.
        """
        stop = 0
        for match in self._added_re.finditer(text) if self._added_re else ():
            start, end = match.span()
            token_id, lstrip, rstrip = self._added[match.group()]
            if lstrip:
                while start > stop and text[start - 1] in _WHITESPACE:
                    start -= 1
            if rstrip:
                while end < len(text) and text[end] in _WHITESPACE:
                    end += 1
            if stop < start:
                yield text[stop:start], None
            yield "", token_id
            stop = end
        if stop < len(text):
            yield text[stop:], None

    def _word_ids(self, word: str) -> tuple[int, ...]:
        n = len(word)
        best_score = [0.0] * (n + 1)
        best_start = [-1] * (n + 1)
        best_id = [0] * (n + 1)
        pieces_get = self._pieces.get
        scores = self._scores
        for start in range(n):
            base = best_score[start]
            single = False
            for length in range(1, min(self._max_piece, n - start) + 1):
                piece_id = pieces_get(word[start : start + length])
                if piece_id is None:
                    continue
                end = start + length
                candidate = base + scores[piece_id]
                if best_start[end] < 0 or candidate > best_score[end]:
                    best_score[end], best_start[end], best_id[end] = candidate, start, piece_id
                if length == 1:
                    single = True
            if not single:
                end = start + 1
                candidate = base + self._unk_score
                if best_start[end] < 0 or candidate > best_score[end]:
                    best_score[end], best_start[end], best_id[end] = candidate, start, self._unk_id
        out: list[int] = []
        end = n
        previous_unk = False
        while end > 0:
            piece_id = best_id[end]
            # Consecutive unknown characters fuse into one unknown piece.
            if not (piece_id == self._unk_id and previous_unk):
                out.append(piece_id)
            previous_unk = piece_id == self._unk_id
            end = best_start[end]
        out.reverse()
        return tuple(out)


def from_hf(hf, *, max_length: int, pad_id: int) -> CompactUnigramTokenizer | None:
    """A compact tokenizer for `hf` when it is the verified shape and agrees, else None.

    `hf` is the loaded tokenizer with truncation at `max_length` and padding
    enabled, as the encoder uses it; the caller drops it when this returns one.
    """
    try:
        compact = _build(hf, max_length, pad_id)
    except Exception:  # noqa: BLE001 - an optimisation must never fail a model load
        log_event(log, logging.WARNING, "compact_tokenizer_fallback", fields={"reason": "build_error"})
        return None
    if compact is None:
        return None
    # Control: this prevents silent tokenization drift, which would make vectors
    # inconsistent with the stored index, if a future `tokenizers` changes Unigram
    # semantics. Fired wrongly it costs today's memory (HF is kept); the operator's
    # capacity pays. The check is content-free in its log.
    if not _agrees(hf, compact, max_length):
        log_event(log, logging.WARNING, "compact_tokenizer_fallback", fields={"reason": "parity"})
        return None
    return compact


def _build(hf, max_length: int, pad_id: int) -> CompactUnigramTokenizer | None:
    spec = json.loads(hf.to_str())
    model = spec.get("model") or {}
    if model.get("type") != "Unigram" or model.get("byte_fallback") or not isinstance(model.get("unk_id"), int):
        return None
    added = spec.get("added_tokens") or []
    if not all(t["special"] and not t["normalized"] and not t["single_word"] for t in added):
        return None
    template = _single_template(spec.get("post_processor"))
    if template is None:
        return None
    prefix, suffix = template
    if len(prefix) + len(suffix) >= max_length:
        return None
    vocab = model["vocab"]
    pieces = {piece: index for index, (piece, _) in enumerate(vocab)}
    if len(pieces) != len(vocab):  # duplicates: the crate's own tie between them is not ported
        return None
    # HF's own parse of each score, which is the one the crate's Viterbi sums.
    scores = array.array("d", (score for _, score in vocab))
    unk_id = model["unk_id"]
    del spec, model, vocab
    return CompactUnigramTokenizer(
        pieces=pieces,
        scores=scores,
        unk_id=unk_id,
        normalizer=hf.normalizer,
        pre_tokenizer=hf.pre_tokenizer,
        added=[(t["content"], t["id"], t["lstrip"], t["rstrip"]) for t in added],
        prefix=prefix,
        suffix=suffix,
        max_length=max_length,
        pad_id=pad_id,
    )


def _single_template(processor: dict | None) -> tuple[list[int], list[int]] | None:
    """Ids of the special tokens before and after `$A` in a TemplateProcessing, if that is all it is."""
    if not processor or processor.get("type") != "TemplateProcessing":
        return None
    specials = processor.get("special_tokens") or {}
    prefix: list[int] = []
    suffix: list[int] = []
    seen_text = False
    for item in processor.get("single") or []:
        if "Sequence" in item:
            if seen_text or item["Sequence"] != {"id": "A", "type_id": 0}:
                return None
            seen_text = True
        elif "SpecialToken" in item:
            token = item["SpecialToken"]
            ids = (specials.get(token["id"]) or {}).get("ids") or []
            if token.get("type_id") != 0 or len(ids) != 1:
                return None
            (suffix if seen_text else prefix).append(ids[0])
        else:
            return None
    return (prefix, suffix) if seen_text and (prefix or suffix) else None


def _probes(max_length: int) -> list[str]:
    return [*_PROBES, "word " * (max_length + 8)]


def _agrees(hf, compact: CompactUnigramTokenizer, max_length: int) -> bool:
    texts = _probes(max_length)
    for batch in (texts, *([text] for text in texts)):
        expected = hf.encode_batch(batch)
        actual = compact.encode_batch(batch)
        for want, got in zip(expected, actual, strict=True):
            if (
                want.ids != got.ids
                or want.attention_mask != got.attention_mask
                or want.type_ids != got.type_ids
                or want.special_tokens_mask != got.special_tokens_mask
                or bool(want.overflowing) != got.overflowing
            ):
                return False
    return True
