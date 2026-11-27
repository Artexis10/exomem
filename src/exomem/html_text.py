"""Turn an HTML body into plain text, for mail and notes whose text is markup.

The stdlib `html.parser` reads the markup, so no regex touches HTML. A block element ends a line, a list item
starts one, a link keeps its text and gains its address, and `script` and `style` are dropped. The text is data:
nothing here follows a link or runs anything. Markup is untrusted, so a fault in the parser degrades to the text
read so far, and the input is cut at `MAX_INPUT_CHARS` because `html.parser` has had inputs that take
super-linear time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from typing import NamedTuple

#: Characters of markup that one conversion reads; longer input is cut there and reported as cut.
MAX_INPUT_CHARS = 4 * 1024 * 1024
#: Whitespace around a link's text that still reads as the text; a link whose text is longer than its address by more is not compared, and keeps its address.
_LINK_PADDING = 64
#: One list of an outer list leads the nested one; each nesting level indents two spaces.
_INDENT = "  "

# nosemgrep: ep-word-set -- The HTML specification's block-level elements: a closed set the specification fixes, not vocabulary.
_BLOCKS = frozenset({
    "address", "article", "aside", "blockquote", "details", "dialog", "dd", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "table", "tr", "ul",
})
# nosemgrep: ep-word-set -- The block elements that stand apart with a blank line, a closed subset of the HTML specification's.
_PARAGRAPHS = frozenset({"blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "ol", "p", "pre", "table", "ul"})
# nosemgrep: ep-word-set -- The two HTML elements whose content is code or style, never text.
_DROPPED = frozenset({"script", "style"})
# nosemgrep: ep-word-set -- The two HTML cell elements that a table row separates.
_CELLS = frozenset({"td", "th"})
# nosemgrep: ep-word-set -- The two HTML list elements, whose items are marked.
_LISTS = frozenset({"ol", "ul"})

#: What replaces a start tag that the markup's own dialect gives a meaning to, such as a note's checkbox: the tag's attributes in, the text out.
Inline = Mapping[str, Callable[[Mapping[str, str | None]], str]]


class Converted(NamedTuple):
    """The text of a markup body, and whether the markup was longer than `MAX_INPUT_CHARS` and was cut there."""

    text: str
    cut: bool


class _Text(HTMLParser):
    def __init__(self, inline: Inline) -> None:
        super().__init__(convert_charrefs=True)
        self._inline = inline
        self._out: list[str] = []
        self._last = "\n"  # the last character written; a document starts as if after a line end
        self._owed = 0  # the line ends owed before the next text
        self._dropped = 0
        self._pre = 0
        self._lists: list[list] = []  # [ordered?, items so far]
        self._written = 0  # the characters in `_out`, so a link's text is measured without joining the output
        self._links: list[tuple[str, int, int]] = []  # each open link: its address, and where its text starts as a piece and as a character
        self._cells = 0

    def _add(self, text: str) -> None:
        if not text:
            return
        if self._owed and self._out:
            self._out.append("\n" * self._owed)
            self._written += self._owed
        self._owed = 0
        self._out.append(text)
        self._written += len(text)
        self._last = text[-1]

    def _break(self, *, blank: bool = False) -> None:
        """Owe a line end, or a blank line; breaks that meet before any text count once, as the larger."""
        self._owed = max(self._owed, 2 if blank else 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._dropped:
            if tag in _DROPPED:
                self._dropped += 1
            return
        if tag in self._inline:
            self._add(self._inline[tag](dict(attrs)))
            return
        if tag in _DROPPED:
            self._dropped += 1
        elif tag == "br":
            self._break()
        elif tag == "a":
            self._links.append((dict(attrs).get("href") or "", len(self._out), self._written))
        elif tag in _LISTS:
            self._lists.append([tag == "ol", 0])
            self._break(blank=not self._lists[:-1])
        elif tag == "li":
            self._break()
            if self._lists:
                self._lists[-1][1] += 1
                mark = f"{self._lists[-1][1]}. " if self._lists[-1][0] else "- "
                self._add(_INDENT * (len(self._lists) - 1) + mark)
        elif tag == "tr":
            self._break()
            self._cells = 0
        elif tag in _CELLS:
            if self._cells:
                self._add("\t")
            self._cells += 1
        elif tag == "pre":
            self._break(blank=True)
            self._pre += 1
        elif tag in _BLOCKS:
            self._break()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._inline and tag not in _DROPPED:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self._dropped:
            if tag in _DROPPED:
                self._dropped -= 1
            return
        if tag == "a" and self._links:
            address, start, written = self._links.pop()
            address = "".join(address.split())
            # The text is read back only when it is about as long as the address, so that closing nested links stays linear.
            shown = "".join(self._out[start:]).strip() if self._written - written <= len(address) + _LINK_PADDING else None
            if address and not address.startswith("#") and address != shown:
                self._add(f" ({address})")
        elif tag in _LISTS:
            if self._lists:
                self._lists.pop()
            self._break(blank=not self._lists)
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._break(blank=True)
        elif tag in _PARAGRAPHS:
            self._break(blank=True)
        elif tag in _BLOCKS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._dropped or not data:
            return
        if self._pre:
            self._add(data)
            return
        words = data.split()
        if not words:
            text = " "
        else:
            text = (" " if data[0].isspace() else "") + " ".join(words) + (" " if data[-1].isspace() else "")
        if text.startswith(" ") and (self._owed or self._last in " \n\t"):
            text = text[1:]
        self._add(text)

    def text(self) -> str:
        lines = "".join(self._out).split("\n")
        kept: list[str] = []
        for line in lines:
            line = line.rstrip()
            if line or (kept and kept[-1]):
                kept.append(line)
        return "\n".join(kept).strip()


def to_text(markup: str, *, inline: Inline | None = None) -> Converted:
    """The text that `markup` shows, without following anything in it.

    `inline` gives a tag its own text, for a dialect whose tags HTML does not know. Markup past `MAX_INPUT_CHARS`
    is left unread and `cut` says so.
    """
    cut = len(markup) > MAX_INPUT_CHARS
    if cut:
        markup = markup[:MAX_INPUT_CHARS]
        # A tag or entity cut in two would show as text.
        if markup.rfind("<") > markup.rfind(">"):
            markup = markup[:markup.rfind("<")]
    reader = _Text(inline or {})
    try:
        reader.feed(markup)
        reader.close()
    except Exception:  # noqa: BLE001 -- markup is a stranger's: whatever it breaks in the parser, the text read so far stands
        pass
    return Converted(reader.text(), cut)
