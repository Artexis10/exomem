"""The files a case puts in a unit's work directory, built at unit time from the case's own spec.

Every file is deterministic: a spec yields the same bytes in every unit and every arm, so a digest that a
check compares means one file. PyMuPDF renders the PDFs and the PNGs, as the product's own reader does.

A spec is ``{"kind": "pdf", "pages": [text, ...], "title": ..., "doi": ...}`` (the title is the document
information title and the DOI is declared in the XMP packet; both are optional), ``{"kind": "text",
"text": ...}`` or ``{"kind": "png", "text": ...}``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_XMP = (
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description rdf:about="" '
    'xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"><prism:doi>{doi}</prism:doi>'
    '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def _write_text(page, text: str, *, fontsize: float, margin: float) -> None:
    import pymupdf  # media extra only; the runner and the core test tier import this module without it

    box = pymupdf.Rect(margin, margin, page.rect.width - margin, page.rect.height - margin)
    if page.insert_textbox(box, text, fontsize=fontsize, fontname="helv") < 0:
        raise ValueError("a fixture's text does not fit its page")


def _pdf(spec: dict) -> bytes:
    import pymupdf  # media extra only

    document = pymupdf.open()
    for text in spec["pages"]:
        _write_text(
            document.new_page(width=pymupdf.paper_rect("a4").width, height=pymupdf.paper_rect("a4").height), text, fontsize=11, margin=56
        )
    # No producer or date: the library would stamp its version and the clock into the bytes.
    document.set_metadata(
        {
            "title": spec.get("title", ""),
            "producer": "",
            "creator": "",
            "creationDate": "",
            "modDate": "",
        }
    )
    if spec.get("doi"):
        document.set_xml_metadata(_XMP.format(doi=spec["doi"]))
    return document.tobytes(garbage=3, deflate=True, no_new_id=True)


def _png(spec: dict) -> bytes:
    import pymupdf  # media extra only

    page = pymupdf.open().new_page(width=480, height=320)
    _write_text(page, spec["text"], fontsize=13, margin=16)
    return page.get_pixmap(dpi=96).tobytes("png")


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
