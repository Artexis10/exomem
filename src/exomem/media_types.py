"""Import-light extension registry for locally extractable artifacts, and the PDF reading rules.

The bounded identity read imports this module in a fresh child process for every captured PDF, so
it stays free of heavy imports.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

AUDIO_EXTS = frozenset(
    {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma", ".opus"}
)
VIDEO_EXTS = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg"}
)
# Camera raw sensor data, decoded by LibRaw rather than Pillow (`image_codecs`).
RAW_EXTS = frozenset({".dng"})
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
) | RAW_EXTS
PDF_EXTS = frozenset({".pdf"})
# Each document extension, the kind it extracts as, and the library that reads that
# kind, by its import name: `extract` dispatches on it and checks that it imports.
_DOCUMENTS = (
    (".docx", "docx", "markitdown"),
    (".xlsx", "xlsx", "markitdown"),
    (".pptx", "pptx", "markitdown"),
    (".html", "html", "markitdown"),
    (".htm", "html", "markitdown"),
    (".epub", "epub", "markitdown"),
    (".odt", "odt", "odfdo"),
    (".ods", "ods", "odfdo"),
    (".odp", "odp", "odfdo"),
    (".rtf", "rtf", "striprtf"),
)
DOC_EXTS: dict[str, str] = {ext: kind for ext, kind, _reader in _DOCUMENTS}
#: The library that reads each document kind.
DOC_READERS: dict[str, str] = {kind: reader for _ext, kind, reader in _DOCUMENTS}
TEXT_EXTS = frozenset({".txt", ".text", ".log"})
EMAIL_EXTS = frozenset({".eml"})
CAL_EXTS = frozenset({".ics"})


_KIND_BY_EXT: dict[str, str] = {
    **dict.fromkeys(AUDIO_EXTS, "audio"),
    **dict.fromkeys(VIDEO_EXTS, "video"),
    **dict.fromkeys(IMAGE_EXTS, "image"),
    **dict.fromkeys(PDF_EXTS, "pdf"),
    **DOC_EXTS,
    **dict.fromkeys(TEXT_EXTS, "text"),
    **dict.fromkeys(EMAIL_EXTS, "email"),
    **dict.fromkeys(CAL_EXTS, "calendar"),
}
#: Every extraction kind this registry assigns to an extension.
KINDS = frozenset(_KIND_BY_EXT.values())


def media_type_for(path: str | Path) -> str | None:
    """Return the deterministic extraction kind for a filename extension."""
    return _KIND_BY_EXT.get(Path(path).suffix.lower())


# A PDF page yielding fewer than this many characters of embedded text is treated as
# scanned → rasterize + OCR fallback.
_PDF_OCR_MIN_CHARS = 16


def pdf_page_text(page: Any) -> str:
    """The text a PDF page's own text layer holds, stripped. A short one means a scan."""
    return page.get_text().strip()


def has_text_layer(page_text: str) -> bool:
    """Whether a page's embedded text is long enough to be read, not rasterized and OCR'd."""
    return len(page_text) >= _PDF_OCR_MIN_CHARS


def pymupdf_allocation_failure(error: BaseException) -> bool:
    """Whether PyMuPDF reports that MuPDF's allocator failed.

    MuPDF raises a failed allocation as FZ_ERROR_SYSTEM (PyMuPDF's `FzErrorSystem`)
    with its own message, `malloc (N bytes) failed` or the calloc and realloc forms.
    Some PyMuPDF calls, page text among them, re-raise it as a RuntimeError that reads
    `code=<FZ_ERROR_SYSTEM>: <message>`. Matching that whole message, never a substring,
    keeps a file path out of it: other system errors name the file.
    """
    import fitz

    mupdf = getattr(fitz, "mupdf", None)
    system_error = getattr(mupdf, "FzErrorSystem", None)
    if system_error is not None and isinstance(error, system_error):
        message = str(getattr(error, "m_text", "") or "")
    elif isinstance(error, RuntimeError) and str(error).startswith(prefix := f"code={getattr(mupdf, 'FZ_ERROR_SYSTEM', '')}: "):
        message = str(error).removeprefix(prefix)
    else:
        return False
    # nosemgrep: ep-word-search -- MuPDF's own allocator message format (source/fitz/memory.c).
    return re.fullmatch(r"(?:malloc|calloc|realloc)(?: of array)? \(\d+(?: x \d+)? bytes\) failed", message) is not None
