"""Timed passages are slices of Source text, addressed in character offsets."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import NamedTuple

from . import semantic_segments
from .vault import in_append_only_tree


def is_source(path: str) -> bool:
    return in_append_only_tree(path) == "Sources"  # the vault layout fixes this lane name


class TranscriptMatch(NamedTuple):
    start: int
    end: int
    text: str
    timestamp: float

    def passage(self) -> dict[str, object]:
        return {"text": self.text, "part": 1, "offset": self.start, "length": self.end - self.start}


def source_text(body: str) -> str:
    """Return the stored text addressed by a transcript citation."""
    from .preserve import extracted_text
    from .source_coverage import CAPTURE_HEADING, fenced_capture

    body = body.replace("\r\n", "\n")
    if text := extracted_text(body):
        return text
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


def match(
    text: str,
    *,
    chunk: str | None,
    title: str,
    query: str,
    withheld: Sequence[tuple[int, int]] = (),
    max_chars: int = 4000,
) -> TranscriptMatch | None:
    """Locate a literal quote or a verified vector chunk in the stored text."""
    spans = turns(text)
    if not spans:
        return None
    query = query.strip().strip('"')
    # Normalize literal whitespace without interpreting the user's intent.
    dense = _locate(text, chunk.removeprefix(f"{title}\n\n")) if chunk else None
    exact = None
    if query and dense is not None:
        low, high = dense
        if inside := _locate(text[low:high], query, ignore_case=True):
            exact = low + inside[0], low + inside[1]
    if query and exact is None:
        exact = _locate(text, query, ignore_case=True)

    for anchor, literal in ((exact, True), (dense, False)):
        if anchor is None:
            continue
        low, high = anchor
        matched = [(start, end, ts) for start, end, ts in spans if start < high and end > low]
        if not matched or (not literal and len(matched) != 1):
            # A legacy vector can name a block with several timed turns.
            # Its score cannot establish which one contains the answer.
            continue
        start, end, timestamp = matched[0]
        for _, turn_end, _ in matched[1:]:
            if turn_end - start > max_chars:
                break
            end = turn_end
        if end <= start + max_chars and not any(begin < end and stop > start for begin, stop in withheld):
            return TranscriptMatch(start, end, text[start:end], timestamp)
    return None


def _locate(text: str, piece: str, *, ignore_case: bool = False) -> tuple[int, int] | None:
    if not ignore_case and piece and (start := text.find(piece)) >= 0:
        return start, start + len(piece)
    words = piece.split()
    if not words:
        return None
    pattern = r"\s+".join(re.escape(word) for word in words)
    found = re.search(pattern, text, re.IGNORECASE if ignore_case else 0)
    return found.span() if found is not None else None


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
