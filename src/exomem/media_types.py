"""Import-light extension registry for locally extractable artifacts, and the PDF reading rules.

The bounded identity read imports this module in a fresh child process for every captured PDF, so
it stays free of heavy imports.
"""

from __future__ import annotations

from collections.abc import Iterator
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
#: The kinds the CLIP index embeds: an image whole, a video by its keyframes.
VISUAL_KINDS = frozenset({"image", "video"})  # nosemgrep: ep-word-set -- a subset of the closed KINDS above, not a vocabulary


def media_type_for(path: str | Path) -> str | None:
    """Return the deterministic extraction kind for a filename extension."""
    return _KIND_BY_EXT.get(Path(path).suffix.lower())


# A PDF page yielding fewer than this many characters of embedded text is treated as
# scanned → rasterize + OCR fallback.
_PDF_OCR_MIN_CHARS = 16


#: The libraries that read a PDF, by import name. PDFium (`pypdfium2`) reads the text layer, renders
#: pages for OCR and reads the information dictionary. `pypdf` reads the XMP packet, which PDFium has no
#: call for, and repairs a damaged file that PDFium cannot open (`damaged_pdf_page_texts`).
PDF_READERS = ("pypdfium2", "pypdf")


def pdf_page_text(page: Any) -> str:
    """The text a PDF page's own text layer holds, stripped. A short one means a scan."""
    text_page = page.get_textpage()
    try:
        # PDFium ends each line with CR LF; the stored text keeps a bare newline. It also writes a hyphen
        # that ends a line as U+0002 and joins the lines; the stored text keeps the printed `-` and break.
        return text_page.get_text_bounded().replace("\r\n", "\n").replace("\x02", "-\n").strip()
    finally:
        text_page.close()


def has_text_layer(page_text: str) -> bool:
    """Whether a page's embedded text is long enough to be read, not rasterized and OCR'd."""
    return len(page_text) >= _PDF_OCR_MIN_CHARS


#: pypdf rebuilds the cross-reference table of a file only after it reads a `startxref`, and a file cut
#: short has none. An empty one appended in memory makes pypdf rebuild it from the objects it finds.
_EMPTY_STARTXREF = b"\nstartxref\n0\n%%EOF\n"


def damaged_pdf_page_texts(path: Path) -> Iterator[str]:
    """The text layer of each page that pypdf can still find in a PDF that PDFium refused to open.

    A page whose content pypdf cannot read gives "", and so does a scanned page, because nothing here
    renders it. Raises when pypdf cannot read the file at all. The pages come in page-tree order, or in
    file order when the tree is unusable, so the count of pages read is not the file's page count.
    """
    import io
    import shutil

    from pypdf import PdfReader

    stream = io.BytesIO()
    with open(path, "rb") as source:
        shutil.copyfileobj(source, stream)
    stream.write(_EMPTY_STARTXREF)
    reader = PdfReader(stream, strict=False)
    try:
        pages = list(reader.pages)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - pypdf fails on a broken page tree in many ways; the objects are listed instead
        pages = []
    for page in pages or _page_objects(reader):
        try:
            yield (page.extract_text() or "").strip()
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - a page whose content stream is damaged is skipped; the rest are read
            yield ""


def _page_objects(reader: Any) -> list[Any]:
    """Every `/Page` object a rebuilt reader holds, in file order, for a file whose page tree is lost."""
    from pypdf import PageObject
    from pypdf.generic import DictionaryObject, IndirectObject

    # The generation 65535 entries are free objects, which hold nothing.
    places = {
        (number, generation): (offset, 0)
        for generation, table in reader.xref.items()
        if generation != 65535
        for number, offset in table.items()
    }
    offsets = {number: place[0] for (number, _generation), place in places.items()}
    # An object inside an object stream has generation 0 and sorts by where its stream starts, then by its place inside it.
    places.update({(number, 0): (offsets.get(stream, 0), inner) for number, (stream, inner) in reader.xref_objStm.items()})
    pages = []
    for (number, generation), _place in sorted(places.items(), key=lambda entry: entry[1]):
        reference = IndirectObject(number, generation, reader)
        try:
            candidate = reader.get_object(reference)
        except MemoryError:
            raise
        except Exception:  # noqa: BLE001 - an object that cannot be parsed is not a page
            continue
        if isinstance(candidate, DictionaryObject) and candidate.get("/Type") == "/Page":
            pages.append(PageObject(reader, reference))
    return pages
