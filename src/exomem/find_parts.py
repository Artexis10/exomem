"""The passage of a Source's extracted text that a search hit matched.

A hit on a Source whose extraction continues on part pages is one document. The hit names
the part that matched (`matched_part`) and carries the matched passage with where it sits in
the whole extracted text (`passage`), so a reader can open that spot with `read_memory`.
"""

from __future__ import annotations

from pathlib import Path

from . import extraction_parts, find_results, preserve
from .embeddings import MAX_WORDS_PER_CHUNK
from .vault import parse_frontmatter, read_guarded_text

#: Characters of surrounding text added on each side of the matched passage: about one
#: paragraph of a hundred words, so the match reads in context without a second read.
CONTEXT_CHARS = 600
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


def matched_passage(
    vault_root: Path,
    matched: str,
    *,
    number: int,
    base: int,
    chunk: str | None,
    query_norm: str,
) -> dict[str, object] | None:
    """`{"matched_part", "passage"}` for a hit on the page `matched`, part `number` of its Source.

    `matched` is a part page, or the Source page for part 1; `base` is where its text starts in
    the whole text. `chunk` is the stored chunk that matched when the hit is semantic. Without
    one, the passage is `MAX_WORDS_PER_CHUNK` words around the first match of the query.
    Returns None when the page cannot be read or holds no text.
    """
    path = vault_root / matched
    try:
        content, _guard = read_guarded_text(vault_root, path)
    except (OSError, UnicodeError):
        return None
    frontmatter = parse_frontmatter(content)[0]
    text = extraction_parts.text_of_part(content) if number > 1 else preserve.extracted_text(content)
    spans = _word_spans(text)
    if not spans:
        return None
    first = end_word = None
    if chunk:
        title = frontmatter.get("title")
        located = _locate_chunk(text, spans, _chunk_words(chunk, title if isinstance(title, str) else None))
        if located is not None:
            first, end_word = located
    if first is None:
        anchor = _anchor_word(text, spans, query_norm) if query_norm else None
        if anchor is None:
            return None
        first = max(0, anchor - MAX_WORDS_PER_CHUNK // 2)
        end_word = min(len(spans), first + MAX_WORDS_PER_CHUNK)
    start, end = _grow(text, spans[first][0], spans[end_word - 1][1])
    return {
        "matched_part": number,
        "passage": {"text": text[start:end], "part": number, "offset": base + start, "length": end - start},
    }
