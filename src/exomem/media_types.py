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
DOC_EXTS: dict[str, str] = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".epub": "epub",
    ".odt": "odt",
    ".ods": "ods",
    ".odp": "odp",
    ".rtf": "rtf",
}
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
