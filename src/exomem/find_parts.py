"""The passage of a Source's extracted text that a search hit matched.

A hit on a Source whose extraction continues on part pages is one document. The hit names
the part that matched (`matched_part`) and carries the matched passage with where it sits in
the whole extracted text (`passage`), so a reader can open that spot with `read_memory`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from . import extraction_parts, find_results, preserve, provenance
from .embeddings import MAX_WORDS_PER_CHUNK
from .vault import parse_frontmatter, read_guarded_text

#: Characters of surrounding text added on each side of the matched passage: about one
#: paragraph of a hundred words, so the match reads in context without a second read.
CONTEXT_CHARS = 600
#: Most characters a passage shows. Most prose chunks of `MAX_WORDS_PER_CHUNK` words fit inside
#: it with both context windows; code and text without word breaks, such as a compact JSON log,
#: can pass it. `read_memory` at the passage's offset returns the rest.
MAX_PASSAGE_CHARS = 4000
#: Words of a stored chunk compared to place it in the text; the chunk's own words, in order,
#: are a literal copy of a run of the text, so a short prefix finds it.
_LOCATE_WORDS = 12


def _word_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for at, character in enumerate(text):
        if character.isspace():
            if start is not None:
                spans.append((start, at))
                start = None
        elif start is None:
            start = at
    if start is not None:
        spans.append((start, len(text)))
    return spans


def _chunk_words(chunk: str, title: str | None) -> list[str]:
    body = chunk
    if title and body.startswith(f"{title}\n\n"):
        body = body[len(title) + 2 :]
    return body.split()


def _locate_chunk(text: str, spans: list[tuple[int, int]], words: list[str]) -> tuple[int, int] | None:
    """The word indices `[first, end)` of the run of `text` that `words` copies, or None."""
    probe = words[:_LOCATE_WORDS]
    if not probe:
        return None
    present = [text[start:end] for start, end in spans]
    for first in range(len(present) - len(probe) + 1):
        if present[first : first + len(probe)] == probe:
            return first, min(len(present), first + len(words))
    return None


def _anchor_word(text: str, spans: list[tuple[int, int]], query_norm: str) -> int | None:
    """The index of the word at the first literal or stem match of the query, or None."""
    anchor = find_results.anchor_in(text, query_norm)
    if anchor is None:
        return None
    for index, (_start, end) in enumerate(spans):
        if end > anchor[0]:
            return index
    return None


def _grow(text: str, start: int, end: int) -> tuple[int, int]:
    """`[start, end)` widened by `CONTEXT_CHARS` on each side, trimmed to whole words."""
    lower = max(0, start - CONTEXT_CHARS)
    while 0 < lower < start and not text[lower - 1].isspace():
        lower += 1
    upper = min(len(text), end + CONTEXT_CHARS)
    while end < upper < len(text) and not text[upper].isspace():
        upper -= 1
    return lower, upper


def _bound(start: int, end: int, focus: int) -> tuple[int, int]:
    """`[start, end)` cut to `MAX_PASSAGE_CHARS` and keeping `focus`, where the match starts.

    The cut opens one context window before `focus`, or earlier when `focus` is near the end.
    """
    lower = max(start, min(focus - CONTEXT_CHARS, end - MAX_PASSAGE_CHARS))
    return lower, lower + MAX_PASSAGE_CHARS


def matched_passage(
    vault_root: Path,
    matched: str,
    *,
    source: str,
    number: int,
    base: int,
    chunk: str | None,
    query_norm: str,
) -> dict[str, object] | None:
    """`{"matched_part", "passage"}` for a hit on the page `matched`, part `number` of `source`.

    `matched` is a part page, or the Source page for part 1; `base` is where its text starts in
    the whole text. `chunk` is the stored chunk that matched when the hit is semantic. Without
    one, the passage is `MAX_WORDS_PER_CHUNK` words around the first match of the query. A
    passage longer than `MAX_PASSAGE_CHARS` is cut to that length around where the query matches
    in it, or around the start of the chunk when the query does not match literally.
    The passage text withholds origin carriers as the hit excerpt does; its `offset` and
    `length` count the stored text. Returns None when the page cannot be read or holds no text.
    """
    path = vault_root / matched
    try:
        content, _guard = read_guarded_text(vault_root, path)
    except (OSError, UnicodeError):
        return None
    title = parse_frontmatter(content)[0].get("title")
    text = extraction_parts.text_of_part(content) if number > 1 else preserve.extracted_text(content)
    return passage_in(
        text,
        number=number,
        base=base,
        chunk=chunk,
        title=title if isinstance(title, str) else None,
        query_norm=query_norm,
        withheld=provenance.withheld_spans(text, owner_path=source),
    )


def passage_in(
    text: str,
    *,
    number: int,
    base: int,
    chunk: str | None,
    title: str | None,
    query_norm: str,
    withheld: Iterable[tuple[int, int]] = (),
    cuts: Sequence[tuple[int, int]] = (),
) -> dict[str, object] | None:
    """`{"matched_part", "passage"}` for a match in `text`, the text of part `number`.

    `base` is where the part starts in the whole stored text, and `title` is the title of the
    page that holds the part, which a semantic `chunk` starts with. The passage text leaves
    out the `withheld` spans of `text`. `cuts` are the ranges of the stored part that `text`
    lacks, ordered: `offset` and `length` count stored characters either way, so the passage
    opens a `read_memory` window at its spot. Returns None when `text` holds no match.
    """
    spans = _word_spans(text)
    if not spans:
        return None
    first = end_word = None
    if chunk:
        located = _locate_chunk(text, spans, _chunk_words(chunk, title))
        if located is not None:
            first, end_word = located
    if first is None:
        anchor = _anchor_word(text, spans, query_norm) if query_norm else None
        if anchor is None:
            return None
        first = max(0, anchor - MAX_WORDS_PER_CHUNK // 2)
        end_word = min(len(spans), first + MAX_WORDS_PER_CHUNK)
    start, end = _grow(text, spans[first][0], spans[end_word - 1][1])
    if end - start > MAX_PASSAGE_CHARS:
        match = find_results.anchor_in(text[start:end], query_norm) if query_norm else None
        start, end = _bound(start, end, start + match[0] if match else spans[first][0])
    shown = extraction_parts.without_spans(text, start, end, withheld)
    stored_start = _stored_position(start, cuts)
    stored_end = _stored_position(end - 1, cuts) + 1 if end > start else stored_start
    return {
        "matched_part": number,
        "passage": {"text": shown, "part": number, "offset": base + stored_start, "length": stored_end - stored_start},
    }


def _stored_position(at: int, cuts: Sequence[tuple[int, int]]) -> int:
    """The stored position of the character at `at` in a text that lacks the ordered `cuts`."""
    for begin, end in cuts:
        if begin > at:
            break
        at += end - begin
    return at
