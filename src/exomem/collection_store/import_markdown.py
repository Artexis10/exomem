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
from markdown_it.rules_block.table import MAX_AUTOCOMPLETED_CELLS, escapedSplit, getLine
from markdown_it.rules_block.table import table as _table_rule

_BOM = b"\xef\xbb\xbf"

# The parser's cost is its token count, and the worst pages of the byte cap (one-cell rows, a wide sparse table)
# carry far more tokens per byte than a ledger. Past this count the page is refused, so a parse stays bounded.
MAX_PARSE_TOKENS = 150_000
# markdown-it-py ends a table early, and turns the rest into a paragraph, once its rows autofill more than this
# many empty cells. Each autofilled cell is three tokens, so a page that reaches that limit is over the token cap
# and is refused whole; a table is never read cut short. The cap must stay under the limit's tokens.
assert MAX_PARSE_TOKENS < 3 * MAX_AUTOCOMPLETED_CELLS


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
    """The page cannot be read as Markdown tables; ``code`` is the import refusal and ``at`` says what to change."""

    def __init__(self, code: str, at: str) -> None:
        super().__init__(code)
        self.code, self.at = code, at


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
    # A cell's text is read as typed from the block pass; the inline pass only builds children nobody reads,
    # and costs up to 6 times as much on a page of brackets.
    parser.disable(["inline"])
    return parser


def read_tables(data: bytes) -> tuple[Table, ...]:
    """Every table of the page in document order; raises :class:`PageError` for a page that cannot be read.

    A job reads the page again for each batch, and a preview reads it twice, so the last few pages' tables are
    kept. The key is the page's own bytes, so a hit is the same page: a changed page never reads a stale table.
    """
    tables, fault = _read(data)
    if fault is not None:
        raise PageError(*fault)
    return tables


@functools.lru_cache(maxsize=4)
def _read(data: bytes) -> tuple[tuple[Table, ...], tuple[str, str] | None]:
    skipped = len(_BOM) if data.startswith(_BOM) else 0
    try:
        text = data[skipped:].decode("utf-8")
    except UnicodeDecodeError:
        return (), ("IMPORT_SOURCE_MALFORMED", "page is not UTF-8 text")
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
    parsed = _parser().parse(text)
    if len(parsed) > MAX_PARSE_TOKENS:
        return (), ("IMPORT_SOURCE_TOO_LARGE", "page has too many cells to read as tables; save the table as csv and import that")
    for token in parsed:
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
    return tuple(tables), None
