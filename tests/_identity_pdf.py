"""Generate small PDFs for the research-source identity tests, with PyMuPDF or as raw objects.

The text is invented and every DOI uses the `10.5555` prefix, which no registrant holds.
A test that needs a PDF calls `pytest.importorskip("fitz")` first, as the extraction tests do.
The hostile builders write raw objects, so a reader meets the bytes exactly as a crafted file holds them.
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


def _write_objects(path: Path, objects: dict[int, bytes], *, info: int | None = None) -> Path:
    """Write numbered PDF objects with their cross-reference table; object 1 is the catalog, `info` the document information."""
    out = bytearray(b"%PDF-1.7\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    size = max(objects) + 1
    table = len(out)
    out += b"xref\n0 %d\n" % size
    for number in range(size):
        out += b"%010d 00000 n \n" % offsets[number] if number in offsets else b"0000000000 65535 f \n"
    trailer = b"/Size %d /Root 1 0 R" % size + (b" /Info %d 0 R" % info if info else b"")
    out += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % table
    path.write_bytes(bytes(out))
    return path


def _stream(dictionary: bytes, data: bytes) -> bytes:
    return b"<< " + dictionary + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def _one_page(content: bytes, resources: bytes) -> dict[int, bytes]:
    return {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources " + resources + b" /Contents 4 0 R >>",
        4: content,
        5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }


def nested_forms_pdf(path: Path, *, depth: int, fanout: int) -> Path:
    """A page that draws a form XObject which draws the next one `fanout` times, `depth` levels deep.

    The file stays a few kilobytes while reading the page takes `fanout ** depth` draws.
    """
    page = b"BT /F1 10 Tf 72 720 Td (A study of harbour lanterns.) Tj ET /X Do"
    objects = _one_page(_stream(b"", page), b"<< /Font << /F1 5 0 R >> /XObject << /X 6 0 R >> >>")
    for level in range(depth):
        number = 6 + level
        last = level == depth - 1
        resources = b"<< >>" if last else b"<< /XObject << /X %d 0 R >> >>" % (number + 1)
        body = b"0 0 1 1 re f" if last else b"/X Do " * fanout
        objects[number] = _stream(b"/Type /XObject /Subtype /Form /BBox [0 0 612 792] /Resources " + resources, body)
    return _write_objects(path, objects)


def text_bomb_pdf(path: Path, *, runs: int) -> Path:
    """A page of `runs` positioned two-glyph text runs, deflated, so extracting its text needs hundreds of megabytes."""
    import zlib

    compressor = zlib.compressobj(9)
    block = b"".join(b"1 0 0 1 %d %d Tm (ab) Tj " % (run % 600, (run * 7) % 790) for run in range(1000))
    parts = [compressor.compress(b"BT /F1 6 Tf ")]
    parts.extend(compressor.compress(block) for _ in range(runs // 1000))
    parts.append(compressor.compress(b"ET") + compressor.flush())
    content = _stream(b"/Filter /FlateDecode", b"".join(parts))
    return _write_objects(path, _one_page(content, b"<< /Font << /F1 5 0 R >> >>"))


def raw_info_pdf(path: Path, info: bytes, page: bytes = b"A study of harbour lanterns.") -> Path:
    """A one-page PDF whose document information dictionary is `info` as written, which can hold strings no PDF library writes."""
    objects = _one_page(_stream(b"", b"BT /F1 10 Tf 72 720 Td (" + page + b") Tj ET"), b"<< /Font << /F1 5 0 R >> >>")
    objects[6] = info
    return _write_objects(path, objects, info=6)
