"""PNG source artifact rendered with Pillow's bundled default bitmap font.

No font files are loaded from the host, so pixel content depends only on the
logical content and the recorded Pillow version.

Degrades like :mod:`membench.artifacts.pdf` when Pillow is absent. It has to:
generation renders every artifact through one choke point, so an unguarded
import here made the media extra a hard dependency of corpus *generation*
rather than of the image artifacts alone — and a clean checkout could not
regenerate the corpus at all.
"""

from __future__ import annotations

import io

from membench.templates.base import SourceContent

_MARGIN = 12
_LINE_HEIGHT = 14
_WIDTH = 640


class ImageUnavailable(RuntimeError):
    pass


def pillow_version() -> str | None:
    try:
        import PIL
    except Exception:  # pragma: no cover - environment dependent
        return None
    return PIL.__version__


def render(content: SourceContent, sentinel: str) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise ImageUnavailable(str(exc)) from exc

    rows: list[str] = [content.title, ""]
    rows.extend(content.lines)
    if content.table:
        rows.append("")
        rows.extend(" | ".join(row) for row in content.table)
    rows.extend(["", sentinel])
    height = _MARGIN * 2 + _LINE_HEIGHT * len(rows)
    canvas = Image.new("RGB", (_WIDTH, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    for index, row in enumerate(rows):
        draw.text((_MARGIN, _MARGIN + index * _LINE_HEIGHT), row, fill=(0, 0, 0))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()
