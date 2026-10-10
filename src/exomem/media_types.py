"""Import-light extension registry for locally extractable artifacts."""

from __future__ import annotations

from pathlib import Path

AUDIO_EXTS = frozenset(
    {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".oga", ".aac", ".wma", ".opus"}
)
VIDEO_EXTS = frozenset(
    {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpeg", ".mpg"}
)
IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic"}
)
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
