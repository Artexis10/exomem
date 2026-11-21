"""Timed passages are slices of Source text, addressed in character offsets."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Sequence
from typing import NamedTuple

from . import note_types, semantic_segments
from .vault import in_append_only_tree, markdown_lf


def is_source(path: str) -> bool:
    return in_append_only_tree(path) == "Sources"  # the vault layout fixes this lane name


def allows_window(note_type: object, basis: note_types.Basis) -> bool:
    """Apply the read endpoint's raw-page eligibility to citations."""
    return basis.selects(str(note_type or "").casefold(), note_types.raw)


class TranscriptMatch(NamedTuple):
    start: int
    end: int
    text: str
    timestamp: float

    def passage(self) -> dict[str, object]:
        return {"text": self.text, "part": 1, "offset": self.start, "length": self.end - self.start}


def source_text(body: str, *, multipart: bool = False, media_sidecar: bool = False) -> str:
    """Return the stored text addressed by a transcript citation."""
    from . import preserve
    from .source_coverage import CAPTURE_HEADING, fenced_capture

    body = markdown_lf(body)
    marker = preserve._EXTRACTED_HEADING
    start = body.find(marker)
    if start >= 0:
        # The citation must use the same extent as the existing extraction reader.
        # Check its first literal marker against Markdown before trusting that extent.
        from .markdown_relations import iter_headings

        if start and body[start - 1] != "\n":
            return ""
        end = body.find("\n", start)
        if end < 0:
            end = len(body)
        if body[start:end].rstrip(" \t") != marker:
            return ""
        lines = body[:end].split("\n")
        if any(line == len(lines) - 1 and level == 2 for line, level, _title in iter_headings(lines)):
            text = preserve.extracted_text(body)
            # An earlier OCR block would make this extent unsuitable for public citations.
            return "" if preserve.SIDECAR_LOW_CONFIDENCE_SENTINEL in text else text
        return ""
    if multipart or media_sidecar or preserve.SIDECAR_LOW_CONFIDENCE_SENTINEL in body:
        return ""
    if (text := fenced_capture(body)) is not None:
        return text
    _, heading, after = f"\n{body}".partition(f"\n{CAPTURE_HEADING}\n")
    return after.strip() if heading else body


def timed_lines(text: str) -> list[tuple[int, int, float]]:
    if "[" not in text:
        return []
    lines: list[tuple[int, int, float]] = []
    start = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("[") and (marker := semantic_segments.TIMED_LINE_RE.match(line)):
            try:
                timestamp = semantic_segments.ts_from_match(marker)
            except (ValueError, OverflowError):
                pass  # Out-of-range tokens are ordinary Source text.
            else:
                lines.append((start, start + len(line.rstrip("\r\n")), timestamp))
        start += len(line)
    return lines


def is_transcript(text: str) -> bool:
    return len(timed_lines(text)) >= 2


def turns(text: str, *, minimum: int = 2) -> list[tuple[int, int, float]]:
    lines = timed_lines(text)
    if len(lines) < minimum:
        return []
    result = []
    for index, (start, _, timestamp) in enumerate(lines):
        end = lines[index + 1][0] if index + 1 < len(lines) else len(text)
        while end > start and text[end - 1].isspace():
            end -= 1
        result.append((start, end, timestamp))
    return result


def chunks(text: str) -> list[str]:
    """Keep each timed turn together, including its continuation."""
    spans = turns(text)
    if not spans:
        return []
    prefix = text[:spans[0][0]].rstrip()
    return ([prefix] if prefix else []) + [text[start:end] for start, end, _ in spans]


def bounded_turns(text: str, *, max_chars: int) -> Iterator[TranscriptMatch]:
    """Split long turns into openable slices with the original turn timestamp."""
    from .embeddings import _hard_cut_point

    for start, end, timestamp in turns(text, minimum=1):
        while end - start > max_chars:
            stop = start + _hard_cut_point(text[start:start + max_chars + 1], max_chars)
            piece = text[start:stop].rstrip()
            yield TranscriptMatch(start, start + len(piece), piece, timestamp)
            start = stop
            while start < end and text[start].isspace():
                start += 1
        if start < end:
            yield TranscriptMatch(start, end, text[start:end], timestamp)


def match(
    text: str,
    *,
    chunk: str | None,
    title: str,
    query: str,
    withheld: Sequence[tuple[int, int]] = (),
) -> TranscriptMatch | None:
    """Locate a literal quote or a verified vector chunk in the stored text."""
    from .find_parts import CONTEXT_CHARS, MAX_PASSAGE_CHARS

    max_chars = MAX_PASSAGE_CHARS
    spans = turns(text)
    if not spans:
        return None
    query = query.strip().strip('"')
    # Normalize literal whitespace without interpreting the user's intent.
    dense = _locate(text, chunk.removeprefix(f"{title}\n\n"), unique=True) if chunk else None
    dense_turn = (
        next((turn for turn in spans if turn[0] <= dense[0] and dense[1] <= turn[1]), None)
        if dense is not None else None
    )
    exact = None
    if query and dense is not None and dense_turn is not None:
        for start, end in (dense, dense_turn[:2]):
            if inside := _locate(text[start:end], query, ignore_case=True):
                exact = start + inside[0], start + inside[1]
                break
    if query and exact is None:
        exact = _locate(text, query, ignore_case=True, unique=True)

    # A legacy vector may cover several turns. Only a literal can then choose one.
    for anchor in (exact, dense if dense_turn is not None else None):
        if anchor is None:
            continue
        low, high = anchor
        matched = [(start, end, ts) for start, end, ts in spans if start < high and end > low]
        if not matched:
            continue
        start, _, timestamp = matched[0]
        end = matched[-1][1]
        if end - start > max_chars:
            low, high = max(start, low), min(end, high)
            if high - low > max_chars:
                continue
            start = max(start, high - max_chars, min(low - CONTEXT_CHARS, end - max_chars))
            end = start + max_chars
        if not any(begin < end and stop > start for begin, stop in withheld):
            return TranscriptMatch(start, end, text[start:end], timestamp)
    return None


def _locate(
    text: str, piece: str, *, ignore_case: bool = False, unique: bool = False,
) -> tuple[int, int] | None:
    words = piece.split()
    if not words:
        return None
    pattern = re.compile(r"\s+".join(re.escape(word) for word in words), re.IGNORECASE if ignore_case else 0)
    found = pattern.search(text)
    if found is None or (unique and pattern.search(text, found.start() + 1) is not None):
        return None  # A legacy vector can carry repeated text without its timestamp.
    return found.span()


def preferred_position(order: Sequence[str], source: str, cites: Callable[[str], bool]) -> int:
    """Keep a verified neutral candidate in the first three and above citing summaries."""
    was_at = next((index for index, path in enumerate(order) if path == source), len(order))
    position = min(2, was_at)
    return next((index for index, path in enumerate(order[:position]) if cites(path)), position)


def cites_path(links: Sequence[str], source: str, *, kb_name: str) -> bool:
    # Authored Source links can be relative to the vault or to the Knowledge Base.
    from .vault import wikilink_page_key

    key = wikilink_page_key(source)
    names = {key, key.removeprefix(f"{kb_name}/")}
    return any(wikilink_page_key(link) in names for link in links)
