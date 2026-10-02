"""CommonMark-owned code and comment locations in the original string."""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from html5lib.html5parser import HTMLParser
from markdown_it import MarkdownIt
from markdown_it.common.utils import escapeHtml
from markdown_it.parser_inline import ParserInline
from markdown_it.renderer import RendererHTML
from markdown_it.rules_inline import backtick, html_inline, image

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
        self.comments: set[int] = set()
        self.offsets: tuple[Span, ...] | None = None
        self.inline_views = iter(())
        self.images: list[tuple[str, int, tuple[Span, ...] | None]] = []
        self.html = []
        self.has_candidates = False
        self.has_checkpoints = False
        self.render_depth = 0
        self.rendered_length = 0
        self.rendered_openers: dict[int, int] = {}
        self.rendered_checkpoints: dict[int, int] = {}

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

    def opener(self, offsets: tuple[Span, ...] | None, start: int) -> int:
        if offsets is None or not 0 <= start < start + 4 <= len(offsets):
            raise MarkdownPositionError("comment opener has no original position")
        begin, end = offsets[start][0], offsets[start + 3][1]
        if self.text[begin:end] != "<!--":
            raise MarkdownPositionError("comment opener has no exact original position")
        return begin


class _HTMLComments(HTMLParser):
    def __init__(self, content: str, locations: _Locations) -> None:
        super().__init__(strict=False, namespaceHTMLElements=True)
        self.content = content
        self.locations = locations
        self.starts = [0] + [ending.end() for ending in re.finditer("\n", content)]

    def reset(self) -> None:
        super().reset()
        # Pinned html5lib 1.1 hook: delegate states and retain tree-driven namespaces.
        declaration = self.tokenizer.markupDeclarationOpenState

        def record_comment():
            matched = declaration()
            if self.tokenizer.state == self.tokenizer.commentStartState:
                start = self.position() - 4
                if self.content[start : start + 4] != "<!--":
                    raise MarkdownPositionError("HTML comment has no exact source opener")
                try:
                    self.locations.comments.add(self.locations.rendered_openers[start])
                except KeyError as error:
                    raise MarkdownPositionError("HTML comment has no original opener") from error
            return matched

        self.tokenizer.markupDeclarationOpenState = record_comment
        data = self.tokenizer.dataState

        def record_data():
            start = self.position()
            if start in self.locations.rendered_checkpoints:
                if not self.content.startswith("&lt;!--", start):
                    raise MarkdownPositionError("malformed comment has no escaped opener")
                self.locations.comments.add(self.locations.rendered_checkpoints[start])
            return data()

        self.tokenizer.dataState = record_data
        if self.tokenizer.state == data:
            self.tokenizer.state = record_data

    def position(self) -> int:
        line, column = self.tokenizer.stream.position()
        if not 1 <= line <= len(self.starts) or column < 0:
            raise MarkdownPositionError("HTML tokenizer has no logical position")
        return self.starts[line - 1] + column


def _ordinary_comment(content: str) -> bool:
    stripped = content.lstrip(" \t")
    if not stripped.startswith("<!--"):
        return False
    closer = stripped.find("-->", 4)
    body = stripped[4:] if closer < 0 else stripped[4:closer]
    return (
        "<" not in body and ">" not in body and (closer < 0 or not stripped[closer + 3 :].strip())
    )


def _record_render(rule):
    def wrapped(tokens, index, options, env):
        locations = env["locations"]
        locations.render_depth += 1
        try:
            fragment = rule(tokens, index, options, env)
        finally:
            locations.render_depth -= 1
        if locations.render_depth:
            return fragment
        token = tokens[index]
        if token.type in {"html_inline", "html_block"}:
            protected = token.meta.get("cdata_end", 0)
            escaped = escapeHtml(fragment[:protected])
            shift = len(escaped) - protected
            fragment = escaped + fragment[protected:]
            offsets = token.meta.get("source_offsets")
            for opener in re.finditer("<!--", token.content[protected:]):
                start = protected + opener.start()
                original = locations.opener(offsets, start)
                locations.rendered_openers[locations.rendered_length + start + shift] = original
        elif token.type == "comment_checkpoint":
            locations.rendered_checkpoints[locations.rendered_length] = token.meta["opener"]
        locations.rendered_length += len(fragment)
        return fragment

    return wrapped


class _RegionRenderer(RendererHTML):
    def __init__(self, parser=None):
        super().__init__(parser)
        self.rules["comment_checkpoint"] = lambda tokens, index, options, env: ""
        self.rules = {kind: _record_render(rule) for kind, rule in self.rules.items()}
        self.renderToken = _record_render(self.renderToken)


def _record_inline(rule, kind: str):
    def wrapped(state, silent):
        start, count = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if not silent:
            locations = state.env["locations"]
            emitted = state.tokens[count:]
            if matched and kind == "html":
                for token in emitted:
                    if token.type != "html_inline":
                        continue
                    if "<!--" in token.content:
                        token.meta["source_offsets"] = locations.offsets[start : state.pos]
                    if not locations.images:
                        locations.html.append(token)
                        locations.has_candidates |= "<!--" in token.content
            elif kind == "html" and state.src.startswith("<!--", start):
                token = state.push("comment_checkpoint", "", 0)
                token.meta["opener"] = locations.opener(locations.offsets, start)
                if not locations.images:
                    locations.has_candidates = locations.has_checkpoints = True
            elif (
                matched and kind == "code" and any(token.type == "code_inline" for token in emitted)
            ):
                locations.code.append(locations.span(start, state.pos))
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
            if any(marker in token.content for marker in ("`", "<!--")):
                if token.map is None:
                    raise MarkdownPositionError("inline content has no source lines")
                views.append(locations.content_offsets(token.content, token.map))
            else:
                views.append(None)
        elif token.type == "html_block":
            locations.html.append(token)
            if token.content.lstrip(" \t").startswith("<![CDATA["):
                terminator = token.content.find("]]>")
                token.meta["cdata_end"] = len(token.content) if terminator < 0 else terminator + 3
            if "<!--" in token.content:
                if token.map is None:
                    raise MarkdownPositionError("HTML block has no source lines")
                token.meta["source_offsets"] = locations.content_offsets(token.content, token.map)
                locations.has_candidates = True
    locations.inline_views = iter(views)


def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": True}, renderer_cls=_RegionRenderer)
    parser.inline = _RegionInline()
    parser.inline.ruler.at("backticks", _record_inline(backtick, "code"))
    parser.inline.ruler.at("html_inline", _record_inline(html_inline, "html"))
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


def scan_markdown(text: str) -> MarkdownRegions:
    """Locate code and rendered HTML ownership at exact original string offsets."""
    if "<!--" not in text and not _may_have_code(text):
        return MarkdownRegions()
    locations = _Locations(text)
    env = {"locations": locations}
    tokens = _PARSER.parse(text, env)
    code = _merge(locations.code)
    if not locations.has_candidates:
        return MarkdownRegions(code)
    if not locations.has_checkpoints and all(
        _ordinary_comment(token.content) for token in locations.html
    ):
        for token in locations.html:
            start = token.content.index("<!--")
            locations.comments.add(locations.opener(token.meta["source_offsets"], start))
    else:
        content = _PARSER.renderer.render(tokens, _PARSER.options, env)
        collapsed = [ending.start() + 1 for ending in re.finditer("\r\n", content)]
        locations.rendered_openers = {
            start - bisect_left(collapsed, start): original
            for start, original in locations.rendered_openers.items()
        }
        locations.rendered_checkpoints = {
            start - bisect_left(collapsed, start): original
            for start, original in locations.rendered_checkpoints.items()
        }
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        _HTMLComments(content, locations).parseFragment(content, scripting=False)
    comments: list[Span] = []
    cursor = 0
    for start in sorted(locations.comments):
        if start < cursor:
            continue
        closer = text.find("-->", start + 4)
        cursor = len(text) if closer < 0 else closer + 3
        comments.append((start, cursor))
    return MarkdownRegions(code, tuple(comments))


def mask_code(text: str, regions: MarkdownRegions | None = None) -> str:
    """Blank code while preserving every original character position and CR/LF."""
    if regions is None:
        if not _may_have_code(text):
            return text
        regions = scan_markdown(text)
    if not regions.code_spans:
        return text
    out = list(text)
    for start, end in regions.code_spans:
        for index in range(start, end):
            if out[index] not in "\r\n":
                out[index] = " "
    return "".join(out)
