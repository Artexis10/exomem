"""The files a case puts in a unit's work directory, built at unit time from the case's own spec.

Every file is deterministic: a spec yields the same bytes in every unit and every arm, so a digest that a
check compares means one file. A small writer sets the PDFs, and Pillow draws the PNGs.

A spec is ``{"kind": "pdf", "pages": [text, ...], "title": ..., "doi": ...}`` (the title is the document
information title and the DOI is declared in the XMP packet; both are optional), ``{"kind": "text",
"text": ...}`` or ``{"kind": "png", "text": ...}``.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from membench.artifacts.pdf import pdf_bytes

_XMP = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"><prism:doi>{doi}</prism:doi>'
    '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


_A4 = (595.276, 841.89)
#: A PNG fixture is 480 x 320 points drawn at 96 dpi, so its sizes are the points scaled by 96 / 72.
_PNG_SIZE = (640, 427)
_PNG_MARGIN = 21
_PNG_FONT_PX = 17
_PNG_LEADING = 21


def _pdf(spec: dict) -> bytes:
    try:
        return pdf_bytes(
            spec["pages"],
            size=_A4,
            margin=56,
            font_size=11,
            info={"Title": spec.get("title", "")},
            xmp=_XMP.format(doi=spec["doi"]) if spec.get("doi") else None,
        )
    except ValueError as error:
        raise ValueError(f"a fixture's text does not fit its page: {error}") from error


def _png(spec: dict) -> bytes:
    # Media extra only: the runner and the core test tier import this module without it.
    from PIL import Image, ImageDraw, ImageFont

    canvas = Image.new("RGB", _PNG_SIZE, color=(255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=_PNG_FONT_PX)
    room = _PNG_SIZE[0] - 2 * _PNG_MARGIN
    lines: list[str] = []
    for paragraph in spec["text"].split("\n"):
        line = ""
        for word in paragraph.split():
            if line and draw.textlength(f"{line} {word}", font=font) > room:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
    if _PNG_MARGIN * 2 + _PNG_LEADING * len(lines) > _PNG_SIZE[1]:
        raise ValueError("a fixture's text does not fit its page")
    for index, line in enumerate(lines):
        draw.text((_PNG_MARGIN, _PNG_MARGIN + index * _PNG_LEADING), line, fill=(0, 0, 0), font=font)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


def _text(spec: dict) -> bytes:
    return spec["text"].encode("utf-8")


_BUILDERS = {"pdf": _pdf, "png": _png, "text": _text}
#: The ``kind`` values a spec may have: the builders above, which a case file cannot extend.
KINDS = frozenset(_BUILDERS)


def build_files(specs: dict[str, dict], directory: Path) -> dict[str, str]:
    """Write each named file into ``directory``. Returns each file's SHA-256, by file name."""
    digests = {}
    for name, spec in specs.items():
        data = _BUILDERS[spec["kind"]](spec)
        (directory / name).write_bytes(data)
        digests[name] = hashlib.sha256(data).hexdigest()
    return digests
