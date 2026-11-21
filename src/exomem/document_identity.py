"""What a captured PDF says about itself, compared with the DOI its `url` claims.

Capture reads the first two pages (no OCR), the document information and the XMP
packet, then `check` compares the DOIs found there with the DOIs in the claimed
`url`. The result says only where a DOI appears. No state means "verified
identity": a citing work can print the cited work's title and DOI, so the calling
agent compares the opening text the reply returns.

`check` is pure. `read_pdf` is the one place that parses an untrusted PDF. It runs only in a
fresh child process that `read_pdf_bounded` caps in memory and time, so a hostile file costs the
capture at most the deadline, and every failure yields an `unavailable` reason instead of failing
the capture.
"""

from __future__ import annotations

import atexit
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from . import media_types

#: The states a check reports; `unavailable` always names its reason.
UNAVAILABLE = "unavailable"
NO_CLAIM = "no_claim"
DECLARED = "declared"
PRINTED = "printed"
CONFLICT = "conflict"
NOT_SHOWN = "not_shown"

#: Why a PDF could not be read: its own condition, never a judgement about its content.
NO_EXTRACTOR = "no_extractor"
ENCRYPTED = "encrypted"
NO_TEXT_LAYER = "no_text_layer"
UNREADABLE = "unreadable"
#: Why the bounded read gave up: it outran its deadline, needed more memory than its cap, or its child died.
TIMEOUT = "timeout"
TOO_LARGE = "too_large"
FAILED = "failed"
_REASONS = frozenset({NO_EXTRACTOR, ENCRYPTED, NO_TEXT_LAYER, UNREADABLE, TIMEOUT, TOO_LARGE, FAILED})

#: The bounds of the child process that reads a PDF. The deadline counts from the request, so the
#: custody write never waits longer for the check.
READ_DEADLINE_SECONDS = 3.0
READ_MEMORY_BYTES = 512 * 1024 * 1024
#: Each read may hold `READ_MEMORY_BYTES`, so a burst of captures waits for a slot within its own
#: deadline instead of multiplying that memory.
READ_SLOTS = 2
_read_slots = threading.BoundedSemaphore(READ_SLOTS)

#: The pages read at capture. Bounded so the cost is independent of the document's length.
PAGES_READ = 2
OPENING_CHARS = 600
#: A packet larger than this is not parsed: XMP is a few kilobytes, so a larger one is hostile.
MAX_XMP_CHARS = 1_000_000
#: The child returns at most this much text per page and per metadata value, so its reply stays
#: small. A real page holds a few thousand characters.
MAX_PAGE_CHARS = 200_000
#: At most this many DOIs and title characters are kept, so a crafted file cannot grow a Source page.
MAX_DOIS = 20
MAX_TITLE_CHARS = 300
#: A match longer than any real DOI suffix is text that only looks like one, so it names no DOI.
MAX_DOI_SUFFIX_CHARS = 300
#: `10.`, a registrant code of at most nine digits, `/` and the longest suffix.
MAX_DOI_CHARS = 3 + 9 + 1 + MAX_DOI_SUFFIX_CHARS

# The DOI syntax of ISO 26324 (`10.`, a registrant code, `/`, a suffix) is fixed by that standard, so
# this regex checks a format and decides nothing about meaning. A text layer may break the line right
# after `10.` or `/`, which the optional breaks join. The suffix stops at whitespace, ASCII and
# typographic closing quotes, URL query or fragment delimiters, and at control, private-use and
# surrogate code points: no DOI holds one, and a page or a metadata string can.
_BREAK = r"(?:[ \t]*\r?\n[ \t]*)?"
_SUFFIX_STOPS = r"\s\"'<>?#&\u201d\u2019\u00bb\u203a\x00-\x1f\x7f-\x9f\ud800-\udfff\ue000-\uf8ff"
_SUFFIX = rf"[^{_SUFFIX_STOPS}]+"
# A line may also break inside the suffix: after `-` or `/`, which never end a sentence, and after `.`
# only before a digit, so `glx.2018.` then `0048` joins while a sentence-ending `.` does not.
_SUFFIX_BREAK = r"(?:(?<=[-/])|(?<=\.)(?=[ \t]*\r?\n[ \t]*\d))[ \t]*\r?\n[ \t]*"
_DOI = re.compile(
    rf"(?<![0-9A-Za-z])10\.{_BREAK}(\d{{4,9}})/{_BREAK}({_SUFFIX}(?:{_SUFFIX_BREAK}{_SUFFIX})*)", re.IGNORECASE
)
_TRAILING = ".,;:!]}"
# Typesetting, not the DOI: a text layer sets a DOI's hyphens as Unicode dashes (U+2010 to U+2015) or a
# minus sign (U+2212), and puts invisible break and direction marks inside a long one. Matching reads
# the hyphens as `-` and drops the marks (soft hyphen, zero-width and bidi controls, word joiners, BOM).
_TYPESET = str.maketrans(
    dict.fromkeys([*map(chr, range(0x2010, 0x2016)), "\u2212"], "-")
    | dict.fromkeys(["\u00ad", *map(chr, range(0x200B, 0x2010)), *map(chr, range(0x202A, 0x202F)),
                     *map(chr, range(0x2060, 0x2070)), "\ufeff"])
)
# A publisher URL names a DOI and then a rendition of it: Springer `.../10.1007/x.pdf`, bioRxiv `...v2`
# or `v1.full.pdf`, Frontiers `/full`, IOP `/pdf`, a trailing `/`. These are identifier and URL format
# shapes (ISO 26324 plus publisher URL layouts), not meaning, so a claimed DOI that extends a found one
# by a path (`/...`), a version (`v<digits>`, then letters-only extensions) or letters-only extensions
# names that DOI. A `.` followed by digits is a further DOI step, not a rendition: a page that wraps
# `...2018.` before `0048` must not let a claimed `...2018.0066` read as printed.
_RENDITION = re.compile(r"/.*|v\d+(?:\.[A-Za-z]+)*|(?:\.[A-Za-z]+)+", re.DOTALL)


@dataclass(frozen=True, slots=True)
class PdfEvidence:
    """What `read_pdf` found, or the reason it found nothing."""

    pages: tuple[str, ...] = ()
    metadata_values: tuple[str, ...] = ()
    title: str | None = None
    unavailable: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityCheck:
    """One check: its state, the DOIs it compared and the opening text it read."""

    state: str
    reason: str | None = None
    claimed_dois: tuple[str, ...] = ()
    declared_dois: tuple[str, ...] = ()
    printed_dois: tuple[str, ...] = ()
    pdf_title: str | None = None
    opening: str = ""

    def frontmatter(self) -> dict[str, Any]:
        """The `identity_check` record a Source page keeps, stamped now: no opening text, which the page body holds."""
        record: dict[str, Any] = {"state": self.state}
        if self.reason:
            record["reason"] = self.reason
        record["claimed_dois"] = list(self.claimed_dois)
        record["declared_dois"] = list(self.declared_dois)
        record["printed_dois"] = list(self.printed_dois)
        if self.pdf_title:
            record["pdf_title"] = self.pdf_title
        record["checked_at"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return record

    def reply(self) -> dict[str, Any]:
        """The `identity` block of a reply file row, with the opening text the agent compares."""
        block: dict[str, Any] = {"state": self.state}
        if self.reason:
            block["reason"] = self.reason
        block["opening"] = self.opening
        if self.pdf_title:
            block["pdf_title"] = self.pdf_title
        block["claimed_dois"] = list(self.claimed_dois)
        block["declared_dois"] = list(self.declared_dois)
        block["printed_dois"] = list(self.printed_dois)
        return block


def reply_block(value: Any) -> dict[str, Any] | None:
    """The `identity` block of a reply row when it reads as `IdentityCheck.reply` writes it, else None.

    A receipt recovered from storage or a producer that drifted is shown as unread, never as a state.
    """
    def text(item: Any, limit: int) -> bool:
        return isinstance(item, str) and len(item) <= limit

    def listed(item: Any) -> bool:
        return (
            isinstance(item, list) and len(item) <= MAX_DOIS
            and all(text(entry, MAX_DOI_CHARS) for entry in item)
        )

    if not isinstance(value, dict) or not text(value.get("state"), 64) or not value["state"]:
        return None
    if not (
        text(value.get("opening"), OPENING_CHARS)
        and all(listed(value.get(key)) for key in ("claimed_dois", "declared_dois", "printed_dois"))
        and all(text(value.get(key), limit) for key, limit in (("reason", 64), ("pdf_title", MAX_TITLE_CHARS)) if key in value)
    ):
        return None
    return {key: value[key] for key in (
        "state", "reason", "opening", "pdf_title", "claimed_dois", "declared_dois", "printed_dois",
    ) if key in value}


def finding(check: Mapping[str, Any]) -> str | None:
    """The sentence that states a `conflict` or `not_shown` check, or None when the check found nothing to state.

    `check` is a reply's `identity` block or a Source page's `identity_check` record. The sentence names
    where DOIs were found and never says the document is or is not the cited work.
    """
    def first(key: str) -> list[str]:
        value = check.get(key)
        return [str(item) for item in value[:3]] if isinstance(value, list) else []

    claimed = ", ".join(first("claimed_dois"))
    if check.get("state") == CONFLICT:
        declared = first("declared_dois")
        shown = declared or first("printed_dois")
        return f"The PDF {'declares' if declared else 'prints'} {', '.join(shown)}, but the url names {claimed}"
    if check.get("state") == NOT_SHOWN:
        return f"The PDF's metadata and first page show no DOI, so the url's {claimed} is unconfirmed"
    return None


def correction_call(ref: str, page_hash: str | None, check: Mapping[str, Any] | None = None) -> str:
    """The step that replaces a Source whose url may not name its document: a metadata-only correction.

    `page_hash` is the guard the call names; None asks the agent to read the page's hash first. When `check`
    is a `conflict` whose document shows exactly one DOI (`_shown_dois`), the call names that DOI and asks for
    the correction when the opening text shows the document is that work. A document that shows two DOIs,
    such as a stale template DOI in its metadata and its own DOI on its first page, gets the comparison instead.
    The comparison names `url ""` for a document with no url of its own, because a correction keeps every field
    it omits (`add.correction_carry`), the wrong url included.
    """
    guard = f'"{page_hash}"' if page_hash else "<the page's content hash from read_page>"
    tail = (
        f'options.correction = {{"supersedes": "{ref}", "expected_hash": {guard}, "reason": "<what was wrong>"}}; '
        "the stored original is kept and nothing is uploaded again."
    )
    shown = _shown_dois(check) if check and check.get("state") == CONFLICT else ()
    if len(shown) == 1:
        return (
            f"If the opening text shows this document is the work {shown[0]} names, correct the Source now, even "
            "when the person gave the url, and tell them what changed: call save_source again with no files, the "
            f'document\'s own title, url "https://doi.org/{shown[0]}" and {tail} '
            "Otherwise tell the person which value is wrong."
        )
    return (
        "Compare the Source's opening text with its url. If the url is wrong, call save_source again with no "
        "files, the document's own title, the right url (url \"\" when you know none for this document: a "
        f"correction keeps every field it leaves out) and {tail}"
    )


def _shown_dois(check: Mapping[str, Any]) -> tuple[str, ...]:
    """The distinct DOIs the document shows, declared before printed, compared without case.

    Metadata can carry a stale or template DOI, so neither field alone names the document's own DOI.
    """
    found: dict[str, None] = {}
    for key in ("declared_dois", "printed_dois"):
        value = check.get(key)
        if isinstance(value, list):
            found.update(dict.fromkeys(str(item).lower() for item in value))
    return tuple(found)


def is_pdf(filename: str) -> bool:
    """Whether a stored original's name says it is a PDF, by the extension set that media processing reads."""
    return Path(filename).suffix.lower() in media_types.PDF_EXTS


def dois_in(text: str) -> tuple[str, ...]:
    """The DOIs a text names, lowercased, in order of appearance and without repeats."""
    found: dict[str, None] = {}
    for match in _DOI.finditer(text.translate(_TYPESET)):
        suffix = _trim(re.sub(r"\s+", "", match.group(2)))
        if suffix and len(suffix) <= MAX_DOI_SUFFIX_CHARS:
            found[f"10.{match.group(1)}/{suffix}".lower()] = None
    return tuple(found)


def _trim(suffix: str) -> str:
    """Drop sentence punctuation and a closing bracket that has no opener in the suffix."""
    while suffix:
        last = suffix[-1]
        if last in _TRAILING or (last == ")" and suffix.count(")") > suffix.count("(")):
            suffix = suffix[:-1]
            continue
        return suffix
    return suffix


def _all_dois(texts: Iterable[str]) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for text in texts:
        found.update(dict.fromkeys(dois_in(text)))
    return tuple(found)[:MAX_DOIS]


def opening_text(pages: Sequence[str]) -> str:
    """The first characters of the pages read, whitespace collapsed."""
    return " ".join(" ".join(pages).split())[:OPENING_CHARS]


def names(claimed: str, found: str) -> bool:
    """Whether a DOI claimed from a URL names the DOI a document shows: the same, or a rendition of it."""
    return claimed == found or (claimed.startswith(found) and _RENDITION.fullmatch(claimed, len(found)) is not None)


def check(
    pages: Sequence[str],
    metadata_values: Sequence[str],
    claimed_url: str | None,
    *,
    pdf_title: str | None = None,
) -> IdentityCheck:
    """Compare the DOIs a document shows with the DOIs its claimed `url` names.

    Only the first page counts as printing a DOI: a short paper's second page can already list its
    references. Both pages feed the opening text. A result that cannot be stored as UTF-8, such as a
    lone surrogate in a page's text, is `unavailable` instead.
    """
    claimed = _all_dois([unquote(claimed_url)] if claimed_url else [])
    found = {
        "claimed_dois": claimed, "declared_dois": _all_dois(metadata_values), "printed_dois": _all_dois(pages[:1]),
        "pdf_title": pdf_title, "opening": opening_text(pages),
    }
    result = IdentityCheck(_state(claimed, found["declared_dois"], found["printed_dois"]), **found)
    try:
        json.dumps(result.reply(), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return unavailable(UNREADABLE, claimed_url)
    return result


def _state(claimed: Sequence[str], declared: Sequence[str], printed: Sequence[str]) -> str:
    if not claimed:
        return NO_CLAIM
    # Metadata is the document's own declaration, so it outranks a DOI that appears in running text.
    for shown, state in ((declared, DECLARED), (printed, PRINTED)):
        if any(names(claim, doi) for claim in claimed for doi in shown):
            return state
        if shown:
            return CONFLICT
    return NOT_SHOWN


def render(record: Mapping[str, Any]) -> str:
    """The one-line YAML flow mapping a Source page stores for an `identity_check` record."""
    return json.dumps(record, ensure_ascii=False)


def after_extraction(metadata: Mapping[str, Any], text: str) -> dict[str, Any] | None:
    """The refreshed `identity_check` record of a Source whose PDF had no text layer at capture, now that OCR read it.

    None for every other Source, and when the extraction holds no text. Only the printed text is
    compared: the capture read no document information from a PDF it could not read.
    """
    from . import extract

    recorded = metadata.get("identity_check")
    if (
        not isinstance(recorded, Mapping)
        or recorded.get("state") != UNAVAILABLE
        or recorded.get("reason") != NO_TEXT_LAYER
        or not text.strip()
    ):
        return None
    url = metadata.get("url")
    return check(extract.leading_pages(text, PAGES_READ), (), url if isinstance(url, str) else None).frontmatter()


def unavailable(reason: str, claimed_url: str | None = None) -> IdentityCheck:
    """The check of a document that could not be read; the capture still stores its original."""
    return IdentityCheck(UNAVAILABLE, reason, claimed_dois=_all_dois([unquote(claimed_url)] if claimed_url else []))


def inspect(path: Path, claimed_url: str | None) -> IdentityCheck:
    """Read the PDF at `path` in a bounded child process and check it against `claimed_url`. Never raises."""
    evidence = read_pdf_bounded(path)
    if evidence.unavailable:
        return unavailable(evidence.unavailable, claimed_url)
    return check(evidence.pages, evidence.metadata_values, claimed_url, pdf_title=evidence.title)


def read_pdf_bounded(path: Path) -> PdfEvidence:
    """`read_pdf` in a child process capped at `READ_MEMORY_BYTES` and answered within `READ_DEADLINE_SECONDS`.

    The deadline, the memory cap and a crash each yield their reason, so a hostile PDF never holds or
    grows the service. Each child reads one PDF and exits.
    """
    if importlib.util.find_spec("fitz") is None:
        # An install without PyMuPDF pays no process start for a read that cannot happen.
        return PdfEvidence(unavailable=NO_EXTRACTOR)
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    if not _read_slots.acquire(timeout=READ_DEADLINE_SECONDS):
        return PdfEvidence(unavailable=TIMEOUT)
    try:
        reader = _take_reader()
        try:
            output, _ = reader.communicate(os.fsencode(path), timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            reader.kill()
            reader.communicate()
            return PdfEvidence(unavailable=TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError):
        return PdfEvidence(unavailable=FAILED)
    finally:
        _read_slots.release()
    return _evidence_from_child(reader.returncode, output)


#: One idle reader that has already imported PyMuPDF, about 100 ms of the read's cost, so a capture
#: pays only the parse. It starts on the first read and is replaced as each read takes it.
_spare: subprocess.Popen | None = None
_spare_lock = threading.Lock()


def _start_reader() -> subprocess.Popen:
    # Its stderr is discarded: a damaged file can make MuPDF warn without end.
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and module; the path arrives on stdin
        [sys.executable, "-P", "-m", "exomem.document_identity", str(READ_MEMORY_BYTES)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _take_reader() -> subprocess.Popen:
    """The idle spare reader when it is alive, else a new one; either way the next spare starts now."""
    global _spare
    with _spare_lock:
        reader, _spare = _spare, _start_reader()
    if reader is None or reader.poll() is not None:
        reader = _start_reader()
    return reader


@atexit.register
def _retire_spare() -> None:
    if _spare is not None and _spare.poll() is None:
        _spare.kill()
        # A killed child exits at once; the budget keeps interpreter exit from waiting on a stuck reap.
        try:
            _spare.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _evidence_from_child(returncode: int, output: bytes) -> PdfEvidence:
    """The evidence the child printed, or `failed` when it died or printed something else."""
    try:
        value = json.loads(output) if returncode == 0 else None
    except ValueError:
        value = None
    if not isinstance(value, dict):
        return PdfEvidence(unavailable=FAILED)
    pages, metadata, title, reason = (value.get(key) for key in ("pages", "metadata_values", "title", "unavailable"))
    if reason is not None:
        return PdfEvidence(unavailable=reason if reason in _REASONS else FAILED)
    if not (
        isinstance(pages, list) and len(pages) <= PAGES_READ and all(isinstance(page, str) for page in pages)
        and isinstance(metadata, list) and all(isinstance(entry, str) for entry in metadata)
        and (title is None or isinstance(title, str))
    ):
        return PdfEvidence(unavailable=FAILED)
    return PdfEvidence(pages=tuple(pages), metadata_values=tuple(metadata), title=title)


def read_pdf(path: Path) -> PdfEvidence:
    """The first pages, document information and XMP text of a PDF, or why none could be read.

    This is the only parse of an untrusted PDF, and it runs only in the child that
    `read_pdf_bounded` starts. One boundary turns every failure into a reason.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return PdfEvidence(unavailable=NO_EXTRACTOR)

    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                return PdfEvidence(unavailable=ENCRYPTED)
            pages = tuple(
                media_types.pdf_page_text(doc[number])[:MAX_PAGE_CHARS]
                for number in range(min(PAGES_READ, doc.page_count))
            )
            if not any(media_types.has_text_layer(text) for text in pages):
                return PdfEvidence(unavailable=NO_TEXT_LAYER)
            information = {
                name: value[:MAX_PAGE_CHARS] for name, value in (doc.metadata or {}).items() if isinstance(value, str)
            }
            try:
                packet = doc.get_xml_metadata() or ""
            except Exception:  # noqa: BLE001 - an XMP stream that inflates past the memory cap is hostile: no packet
                packet = ""
    except Exception as error:  # noqa: BLE001 - PyMuPDF raises many types for damaged input; each means unreadable
        exhausted = isinstance(error, MemoryError) or media_types.pymupdf_allocation_failure(error)
        return PdfEvidence(unavailable=TOO_LARGE if exhausted else UNREADABLE)
    # A title is untrusted text that the Source page stores in frontmatter: keep printable characters only.
    title = "".join(ch for ch in " ".join((information.get("title") or "").split()) if ch.isprintable())
    title = title[:MAX_TITLE_CHARS].strip() or None
    return PdfEvidence(
        pages=pages,
        metadata_values=(*information.values(), *xmp_values(packet)),
        title=title,
    )


def xmp_values(packet: str) -> tuple[str, ...]:
    """Every text and attribute value of an XMP packet, parsed with `defusedxml`.

    No namespace is named: a DOI can sit in `prism:doi`, `dc:identifier` or a publisher's own
    field, and the DOI syntax finds it in any of them. A packet that is oversized, malformed or
    refused by `defusedxml` (entity expansion, external entities) counts as no packet.
    """
    if not packet.strip() or len(packet) > MAX_XMP_CHARS:
        return ()
    try:
        from defusedxml import ElementTree

        root = ElementTree.fromstring(packet)
    except Exception:  # noqa: BLE001 - defusedxml raises its own refusals beside ParseError; no parser means no packet
        return ()
    values: list[str] = []
    for node in root.iter():
        if node.text and node.text.strip():
            values.append(node.text.strip())
        values.extend(value for value in node.attrib.values() if value.strip())
    return tuple(values)


def _child(argv: Sequence[str]) -> int:
    """A reader: cap the data segment, import PyMuPDF, then read the one PDF named on stdin and print JSON."""
    from . import media_brakes

    try:
        media_brakes.cap_data_segment(int(argv[0]))
    except ImportError:
        pass  # No RLIMIT on this platform (Windows): the parent's deadline still bounds the read.
    try:
        import fitz  # noqa: F401 - imported before the request, so a spare reader answers at once
    except ImportError:
        pass
    request = sys.stdin.buffer.read()
    if not request:
        return 0  # The parent exited before it needed this spare.
    try:
        evidence = read_pdf(Path(os.fsdecode(request)))
    except MemoryError:
        evidence = PdfEvidence(unavailable=TOO_LARGE)
    payload = {
        "pages": list(evidence.pages), "metadata_values": list(evidence.metadata_values),
        "title": evidence.title, "unavailable": evidence.unavailable,
    }
    # ASCII escapes carry any code point, a lone surrogate included, through the pipe unchanged.
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=True).encode("ascii"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    # The reply is written and flushed; skipping interpreter teardown saves the reader's exit a few milliseconds.
    os._exit(_child(sys.argv[1:]))
