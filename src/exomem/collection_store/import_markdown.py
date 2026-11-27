"""The tables of one preserved Markdown page, for the ``markdown-table`` import format.

markdown-it-py reads the table syntax, so a cell splits, unescapes and pads exactly as the page renders:
a row with fewer cells than the header reads the missing cells as empty. The parser drops a cell beyond
the header without a trace, so the table rule is wrapped to keep what it drops: each row carries the cells
beyond the header, split by the rule's own splitter, and the importer rejects a row whose dropped cells hold
text. This module otherwise walks the parser's tokens. It keeps each cell's text as written (inline markup
stays as typed) and each row's byte span in the page, so a row reports where it sits in its source.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_block.table import escapedSplit, getLine
from markdown_it.rules_block.table import table as _table_rule

_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True, slots=True)
class TableRow:
    cells: tuple[str, ...]
    start: int  # byte offset of the row's line in the page
    end: int  # byte offset after the row's line
    beyond: tuple[str, ...]  # the row's cells past the header's width, which the parser drops


@dataclass(frozen=True, slots=True)
class Table:
    header: tuple[str, ...]
    rows: tuple[TableRow, ...]


class PageError(Exception):
    """The page cannot be read as Markdown tables; ``code`` is the import refusal that names why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _table_keeping_dropped_cells(state: Any, start: int, end: int, silent: bool) -> bool:
    """markdown-it-py's table rule, then each body row's cells beyond the header on its ``tr_open`` token.

    The rule splits a row's line with ``escapedSplit`` and drops the cells beyond the header's width. This
    splits the same line the same way, inside the same container (a quote or a list item shifts the line's
    start), so the count is the parser's own and no pipe is counted by hand."""
    first = len(state.tokens)
    matched = _table_rule(state, start, end, silent)
    if not matched or silent:
        return matched
    width = sum(token.type == "th_open" for token in state.tokens[first:])
    for token in state.tokens[first:]:
        if token.type == "tr_open" and token.map[0] > start + 1:  # a body row; the header and delimiter rows come first
            cells = escapedSplit(getLine(state, token.map[0]).strip())
            if cells and cells[0] == "":
                cells.pop(0)
            if cells and cells[-1] == "":
                cells.pop()
            token.meta["beyond"] = tuple(cell.strip() for cell in cells[width:])
    return matched


@functools.cache
def _parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark").enable("table")
    parser.block.ruler.at("table", _table_keeping_dropped_cells, {"alt": ["paragraph", "reference"]})
    return parser


def read_tables(data: bytes) -> tuple[Table, ...]:
    """Every table of the page in document order; raises :class:`PageError` for bytes that are not UTF-8."""
    skipped = len(_BOM) if data.startswith(_BOM) else 0
    try:
        text = data[skipped:].decode("utf-8")
    except UnicodeDecodeError as error:
        raise PageError("IMPORT_SOURCE_MALFORMED") from error
    # The parser counts a line at \n, \r\n or \r, the breaks bytes.splitlines also honours.
    starts, offset = [], skipped
    for line in data[skipped:].splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    starts.append(offset)

    def span(token: Any) -> tuple[int, int]:
        first, after = token.map
        return starts[first], starts[min(after, len(starts) - 1)]

    tables: list[Table] = []
    header: tuple[str, ...] = ()
    rows: list[TableRow] = []
    cells: list[str] = []
    place, beyond = (0, 0), ()
    in_head = in_row = False
    for token in _parser().parse(text):
        match token.type:  # markdown-it-py's own token types: the parser fixes this set
            case "table_open":
                header, rows = (), []
            case "thead_open" | "thead_close":
                in_head = token.type == "thead_open"
            case "tr_open":
                cells, place, beyond, in_row = [], span(token), tuple(token.meta.get("beyond", ())), True
            case "inline" if in_row:  # a header or body cell's text as written
                cells.append(token.content)
            case "tr_close":
                in_row = False
                if in_head:
                    header = tuple(cells)
                else:
                    rows.append(TableRow(tuple(cells), *place, beyond))
            case "table_close":
                tables.append(Table(header, tuple(rows)))
    return tuple(tables)
