"""PDF source artifact, written here with no PDF library.

Determinism: the bytes hold no clock, no library version and no random identifier, so one input gives
one digest in every unit. Text is set in the base-14 Helvetica font, so the file needs no embedded font.
"""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence

from membench.templates.base import SourceContent

#: The average advance of Helvetica text as a fraction of the font size. Line breaking needs a safe
#: bound, not exact widths; a line that runs a little short of the margin is harmless.
_ADVANCE = 0.55
_LEADING = 1.2
_LETTER = (612.0, 792.0)


class PdfUnavailable(RuntimeError):
    pass


def _literal(text: str) -> bytes:
    data = text.encode("cp1252")  # the font's WinAnsi encoding; anything else is refused below
    return b"(" + data.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def _text_string(value: str) -> bytes:
    return b"<FEFF" + value.encode("utf-16-be").hex().upper().encode() + b">"


def _lines(text: str, width: float, font_size: float) -> list[str]:
    columns = max(int(width / (font_size * _ADVANCE)), 1)
    lines: list[str] = []
    for paragraph in text.split("\n"):
        # Words stay whole: a DOI or a sentinel that broke across lines would no longer read as one token.
        lines.extend(textwrap.wrap(paragraph, columns, break_long_words=False, break_on_hyphens=False) or [""])
    return lines


def pdf_bytes(
    pages: Sequence[str],
    *,
    size: tuple[float, float] = _LETTER,
    margin: float = 72.0,
    font_size: float = 11.0,
    info: Mapping[str, str] | None = None,
    xmp: str | None = None,
) -> bytes:
    """A PDF with one page per text. `info` names document information entries (`Title`, `Producer`).

    Raises ValueError when a page's text does not fit its page or has a character the font cannot set.
    """
    width, height = size
    capacity = int((height - 2 * margin) // (font_size * _LEADING))
    objects: dict[int, bytes] = {3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"}
    kids: list[bytes] = []
    for index, text in enumerate(pages):
        lines = _lines(text, width - 2 * margin, font_size)
        if len(lines) > capacity:
            raise ValueError(f"page {index + 1} holds {len(lines)} lines and the page fits {capacity}")
        page, content = 4 + 2 * index, 5 + 2 * index
        kids.append(b"%d 0 R" % page)
        objects[page] = (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %g %g] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % (width, height, content)
        )
        shown = b" T* ".join(_literal(line) + b" Tj" for line in lines)
        stream = b"BT /F1 %g Tf %g TL %g %g Td %s ET" % (
            font_size, font_size * _LEADING, margin, height - margin - font_size, shown,
        )
        objects[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(kids) + b"] /Count %d >>" % len(pages)
    spare = 4 + 2 * len(pages)
    catalog = b"<< /Type /Catalog /Pages 2 0 R"
    if xmp:
        packet = xmp.encode()
        objects[spare] = b"<< /Type /Metadata /Subtype /XML /Length %d >>\nstream\n%s\nendstream" % (len(packet), packet)
        catalog += b" /Metadata %d 0 R" % spare
        spare += 1
    objects[1] = catalog + b" >>"
    trailer = b"/Root 1 0 R"
    entries = {key: value for key, value in (info or {}).items() if value}
    if entries:
        objects[spare] = b"<< " + b" ".join(b"/%s %s" % (key.encode(), _text_string(value)) for key, value in entries.items()) + b" >>"
        trailer += b" /Info %d 0 R" % spare
    body = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(body)
        body += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    xref = len(body)
    count = max(objects) + 1
    table = b"xref\n0 %d\n0000000000 65535 f \n" % count
    table += b"".join(b"%010d 00000 n \n" % offsets[number] for number in range(1, count))
    return body + table + b"trailer\n<< /Size %d %s >>\nstartxref\n%d\n%%%%EOF\n" % (count, trailer, xref)


def render(content: SourceContent, sentinel: str) -> bytes:
    text_lines = [content.title, ""]
    text_lines.extend(content.lines)
    if content.table:
        text_lines.append("")
        text_lines.extend(" | ".join(row) for row in content.table)
    text_lines.extend(["", sentinel])
    try:
        return pdf_bytes(
            ["\n".join(text_lines)],
            info={
                "CreationDate": "D:20250106000000Z",
                "ModDate": "D:20250106000000Z",
                "Producer": "membench",
                "Creator": "membench",
            },
        )
    except ValueError as exc:
        raise PdfUnavailable(str(exc)) from exc
