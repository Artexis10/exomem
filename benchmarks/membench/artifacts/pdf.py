"""PDF source artifact via pymupdf when importable; degrades otherwise.

Determinism: creation/modification metadata is pinned so bytes do not depend
on wall-clock time.
"""

from __future__ import annotations

from membench.templates.base import SourceContent


class PdfUnavailable(RuntimeError):
    pass


def pymupdf_version() -> str | None:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - environment dependent
        return None
    version = getattr(fitz, "version", None)
    return version[0] if version else "unknown"


def render(content: SourceContent, sentinel: str) -> bytes:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception as exc:
        raise PdfUnavailable(str(exc)) from exc
    doc = fitz.open()
    page = doc.new_page()
    text_lines = [content.title, ""]
    text_lines.extend(content.lines)
    if content.table:
        text_lines.append("")
        text_lines.extend(" | ".join(row) for row in content.table)
    text_lines.extend(["", sentinel])
    page.insert_text((72, 72), "\n".join(text_lines), fontsize=11)
    doc.set_metadata(
        {
            "creationDate": "D:20250106000000Z",
            "modDate": "D:20250106000000Z",
            "producer": "membench",
            "creator": "membench",
        }
    )
    data = doc.tobytes(deflate=True, garbage=4)
    doc.close()
    return data
