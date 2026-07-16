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
}
TEXT_EXTS = frozenset({".txt", ".text", ".log"})
EMAIL_EXTS = frozenset({".eml"})
CAL_EXTS = frozenset({".ics"})


def media_type_for(path: str | Path) -> str | None:
    """Return the deterministic extraction kind for a filename extension."""
    ext = Path(path).suffix.lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOC_EXTS:
        return DOC_EXTS[ext]
    if ext in TEXT_EXTS:
        return "text"
    if ext in EMAIL_EXTS:
        return "email"
    if ext in CAL_EXTS:
        return "calendar"
    return None
