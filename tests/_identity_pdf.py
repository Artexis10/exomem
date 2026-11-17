"""Generate small PDFs with PyMuPDF for the research-source identity tests.

The text is invented and every DOI uses the `10.5555` prefix, which no registrant holds.
A test that needs a PDF calls `pytest.importorskip("fitz")` first, as the extraction tests do.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

XMP_HEAD = (
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
)
XMP_TAIL = "</rdf:RDF></x:xmpmeta>"


def xmp_with_doi(doi: str) -> str:
    """An XMP packet that declares `doi` as an attribute, the way publisher tools write `prism:doi`."""
    return (
        f'{XMP_HEAD}<rdf:Description xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/" '
        f'prism:doi="{doi}"/>{XMP_TAIL}'
    )


def make_pdf(
    path: Path,
    pages: Sequence[str] = ("A study of harbour lanterns. Invented text for a test document.",),
    *,
    info: dict[str, str] | None = None,
    xmp: str | None = None,
    password: str | None = None,
) -> Path:
    """Write a PDF whose page `n` prints `pages[n]`; an empty string leaves that page with no text layer."""
    import fitz

    with fitz.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text, fontsize=10)
        if info:
            doc.set_metadata(info)
        if xmp:
            doc.set_xml_metadata(xmp)
        if password:
            doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
        else:
            doc.save(path)
    return path
