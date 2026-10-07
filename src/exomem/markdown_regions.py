"""CommonMark-owned code locations, and comment spans outside them.

markdown-it owns code: fenced and indented blocks and inline code spans, at
exact original offsets. It is used only to say where code is; wikilink
scanning keeps its own line-regex masker on every page.

`outside` is the one rule for "not code and not backslash-escaped". Origin
carriers apply it to each reserved opener directly (see `provenance`), so no
other comment can hide one. `comment_spans` is the provenance tag scanner's
view: every `<!--` outside code opens a span to its first `-->`.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.parser_inline import ParserInline
from markdown_it.rules_inline import backtick, image

Span = tuple[int, int]


@dataclass(frozen=True, slots=True)
class MarkdownRegions:
    code_spans: tuple[Span, ...] = ()
    comment_spans: tuple[Span, ...] = ()


class MarkdownPositionError(ValueError):
    """A parser-owned position could not be bound to the original document."""


def _may_have_code(text: str) -> bool:
    return any(marker in text for marker in ("`", "~", "\t", "    "))


class _Locations:
    def __init__(self, text: str) -> None:
        self.text = text
        self.starts = [0]
        self.ends = []
        for ending in re.finditer(r"\r\n|\r|\n", text):
            self.ends.append(ending.start())
            self.starts.append(ending.end())
        self.ends.append(len(text))
        self.code: list[Span] = []
        self.offsets: tuple[Span, ...] | None = None
        self.inline_views = iter(())
        self.images: list[tuple[str, int, tuple[Span, ...] | None]] = []

    def content_offsets(self, content: str, lines: list[int]) -> tuple[Span, ...]:
        """Align pre-inline source within its own original physical lines only."""
        offsets = []
        pieces = content.split("\n")
        for index, piece in enumerate(pieces):
            if not piece and index == len(pieces) - 1:
                break
            line = lines[0] + index
            if line >= lines[1] or line >= len(self.starts):
                raise MarkdownPositionError("parser content exceeds its source lines")
            begin, end = self.starts[line], self.ends[line]
            physical = self.text[begin:end].replace("\0", "\ufffd")
            column = physical.find(piece)
            padding = 0
            selected = piece
            if column < 0:
                # Container indentation can expand a partially consumed tab.
                stripped = piece.lstrip(" \t")
                padding = len(piece) - len(stripped)
                column = physical.find(stripped) if stripped else len(physical)
                selected = stripped
            if column < 0 or (selected and physical.find(selected, column + 1) >= 0):
                raise MarkdownPositionError("parser content has no unique line-local position")
            offsets.extend((begin + column, begin + column) for _ in range(padding))
            offsets.extend(
                (begin + column + char, begin + column + char + 1)
                for char in range(len(piece) - padding)
            )
            if index < len(pieces) - 1:
                if line + 1 >= len(self.starts):
                    raise MarkdownPositionError("parser newline has no original line ending")
                offsets.append((end, self.starts[line + 1]))
        if len(offsets) != len(content):
            raise MarkdownPositionError("parser content mapping is incomplete")
        return tuple(offsets)

    def span(self, start: int, end: int) -> Span:
        if self.offsets is None or not 0 <= start < end <= len(self.offsets):
            raise MarkdownPositionError("inline region has no original position")
        return self.offsets[start][0], self.offsets[end - 1][1]


def _record_code(rule):
    def wrapped(state, silent):
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if (
            matched
            and not silent
            and any(token.type == "code_inline" for token in state.tokens[count:])
        ):
            state.env["locations"].code.append(state.env["locations"].span(start, state.pos))
        return matched

    return wrapped


def _record_image(state, silent):
    locations = state.env["locations"]
    locations.images.append((state.src, state.pos + 2, locations.offsets))
    try:
        return image(state, silent)
    finally:
        locations.images.pop()


class _RegionInline(ParserInline):
    def parse(self, src, md, env, tokens):
        locations = env["locations"]
        previous = locations.offsets
        if locations.images:
            parent, start, offsets = locations.images[-1]
            if parent[start : start + len(src)] != src:
                raise MarkdownPositionError("image label has no exact parent source")
            locations.offsets = None if offsets is None else offsets[start : start + len(src)]
        else:
            try:
                locations.offsets = next(locations.inline_views)
            except StopIteration as error:
                raise MarkdownPositionError("inline content has no captured source") from error
        try:
            return super().parse(src, md, env, tokens)
        finally:
            locations.offsets = previous


def _prepare_regions(state) -> None:
    locations = state.env["locations"]
    views = []
    for token in state.tokens:
        if token.type in {"code_block", "fence"}:
            if token.map is None:
                raise MarkdownPositionError("code block has no source lines")
            start, end = token.map
            locations.code.append(
                (
                    locations.starts[start],
                    locations.starts[end] if end < len(locations.starts) else len(locations.text),
                )
            )
        elif token.type == "inline":
            if "`" in token.content:
                if token.map is None:
                    raise MarkdownPositionError("inline content has no source lines")
                views.append(locations.content_offsets(token.content, token.map))
            else:
                views.append(None)
    locations.inline_views = iter(views)


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": True})
    parser.inline = _RegionInline()
    parser.inline.ruler.at("backticks", _record_code(backtick))
    parser.inline.ruler.at("image", _record_image)
    parser.core.ruler.before("inline", "regions", _prepare_regions)
    parser.core.ruler.disable("text_join")
    return parser


_PARSER = _parser()


def _merge(spans: list[Span]) -> tuple[Span, ...]:
    merged: list[Span] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _escaped(text: str, index: int) -> bool:
    slashes = 0
    while index > slashes and text[index - slashes - 1] == "\\":
        slashes += 1
    return slashes % 2 == 1


def code_spans(text: str) -> tuple[Span, ...]:
    """Every code region's exact original offsets; empty when no code marker exists."""
    if not _may_have_code(text):
        return ()
    locations = _Locations(text)
    _PARSER.parse(text, {"locations": locations})
    return _merge(locations.code)


def outside(text: str, index: int, code: tuple[Span, ...]) -> bool:
    """Whether `index` is neither inside code nor backslash-escaped."""
    owner = bisect_right([start for start, _end in code], index) - 1
    return not (owner >= 0 and index < code[owner][1]) and not _escaped(text, index)


def _comments(text: str, code: tuple[Span, ...]) -> tuple[Span, ...]:
    comments: list[Span] = []
    cursor = 0
    for opener in re.finditer("<!--", text):
        start = opener.start()
        if start < cursor or not outside(text, start, code):
            continue
        closer = text.find("-->", start + 4)
        cursor = len(text) if closer < 0 else closer + 3
        comments.append((start, cursor))
    return tuple(comments)


def scan_markdown(text: str) -> MarkdownRegions:
    """Locate code, then comment spans outside it, at exact original offsets."""
    code = code_spans(text)
    if "<!--" not in text:
        return MarkdownRegions(code)
    return MarkdownRegions(code, _comments(text, code))
