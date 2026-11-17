"""What a captured PDF says about itself, compared with the DOI its `url` claims.

Capture reads the first two pages (no OCR), the document information and the XMP
packet, then `check` compares the DOIs found there with the DOIs in the claimed
`url`. The result says only where a DOI appears. No state means "verified
identity": a citing work can print the cited work's title and DOI, so the calling
agent compares the opening text the reply returns.

`check` is pure. `read_pdf` is the one place that parses an untrusted PDF, and any
failure there yields an `unavailable` reason instead of failing the capture.
"""

from __future__ import annotations

import datetime as dt
import json
import re
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

#: The pages read at capture. Bounded so the cost is independent of the document's length.
PAGES_READ = 2
OPENING_CHARS = 600
#: A packet larger than this is not parsed: XMP is a few kilobytes, so a larger one is hostile.
MAX_XMP_CHARS = 1_000_000
#: At most this many DOIs and title characters are kept, so a crafted file cannot grow a Source page.
MAX_DOIS = 20
MAX_TITLE_CHARS = 300
#: A match longer than any real DOI suffix is text that only looks like one, so it names no DOI.
MAX_DOI_SUFFIX_CHARS = 300
#: `10.`, a registrant code of at most nine digits, `/` and the longest suffix.
MAX_DOI_CHARS = 3 + 9 + 1 + MAX_DOI_SUFFIX_CHARS

# The DOI syntax of ISO 26324 (`10.`, a registrant code, `/`, a suffix) is fixed by that standard, so
# this regex checks a format and decides nothing about meaning. A text layer may break the line right
# after `10.` or `/`, which the optional breaks join. The suffix stops at whitespace, quotes and URL
# query or fragment delimiters.
_BREAK = r"(?:[ \t]*\r?\n[ \t]*)?"
_DOI = re.compile(rf"(?<![0-9A-Za-z])10\.{_BREAK}(\d{{4,9}})/{_BREAK}([^\s\"'<>?#&]+)", re.IGNORECASE)
_TRAILING = ".,;:!]}"


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


def finding(block: dict[str, Any]) -> str | None:
    """The sentence that states a `conflict` or `not_shown` check, or None when the check found nothing to state.

    It names where DOIs were found and never says the document is or is not the cited work.
    """
    claimed = ", ".join(block["claimed_dois"][:3])
    if block["state"] == CONFLICT:
        shown = block["declared_dois"] or block["printed_dois"]
        where = "declares" if block["declared_dois"] else "prints"
        return f"The PDF {where} {', '.join(shown[:3])}, but the url names {claimed}"
    if block["state"] == NOT_SHOWN:
        return f"The PDF shows no DOI, so the url's {claimed} is unconfirmed"
    return None


def is_pdf(filename: str) -> bool:
    """Whether a stored original's name says it is a PDF, by the extension set that media processing reads."""
    return Path(filename).suffix.lower() in media_types.PDF_EXTS


def dois_in(text: str) -> tuple[str, ...]:
    """The DOIs a text names, lowercased, in order of appearance and without repeats."""
    found: dict[str, None] = {}
    for match in _DOI.finditer(text):
        suffix = _trim(match.group(2))
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


def check(
    pages: Sequence[str],
    metadata_values: Sequence[str],
    claimed_url: str | None,
    *,
    pdf_title: str | None = None,
) -> IdentityCheck:
    """Compare the DOIs a document shows with the DOIs its claimed `url` names."""
    claimed = _all_dois([unquote(claimed_url)] if claimed_url else [])
    opening = opening_text(pages)
    if not claimed:
        return IdentityCheck(NO_CLAIM, pdf_title=pdf_title, opening=opening)
    declared = _all_dois(metadata_values)
    printed = _all_dois(pages)
    found = {"claimed_dois": claimed, "declared_dois": declared, "printed_dois": printed,
             "pdf_title": pdf_title, "opening": opening}
    # Metadata is the document's own declaration, so it outranks a DOI that appears in running text.
    if set(claimed) & set(declared):
        return IdentityCheck(DECLARED, **found)
    if declared:
        return IdentityCheck(CONFLICT, **found)
    if set(claimed) & set(printed):
        return IdentityCheck(PRINTED, **found)
    if printed:
        return IdentityCheck(CONFLICT, **found)
    return IdentityCheck(NOT_SHOWN, **found)


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
    opening = text.split(f"\n{extract.page_marker(PAGES_READ + 1)}\n", 1)[0]
    url = metadata.get("url")
    return check((opening,), (), url if isinstance(url, str) else None).frontmatter()


def unavailable(reason: str, claimed_url: str | None = None) -> IdentityCheck:
    """The check of a document that could not be read; the capture still stores its original."""
    return IdentityCheck(UNAVAILABLE, reason, claimed_dois=_all_dois([unquote(claimed_url)] if claimed_url else []))


def inspect(path: Path, claimed_url: str | None) -> IdentityCheck:
    """Read the PDF at `path` and check it against `claimed_url`. Never raises."""
    evidence = read_pdf(path)
    if evidence.unavailable:
        return unavailable(evidence.unavailable, claimed_url)
    return check(evidence.pages, evidence.metadata_values, claimed_url, pdf_title=evidence.title)


def read_pdf(path: Path) -> PdfEvidence:
    """The first pages, document information and XMP text of a PDF, or why none could be read.

    This is the only parse of an untrusted PDF in the request path. One boundary turns every
    failure into a reason, so a hostile or damaged file costs the capture nothing but the check.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return PdfEvidence(unavailable=NO_EXTRACTOR)
    from . import extract

    try:
        with fitz.open(path) as doc:
            if doc.needs_pass:
                return PdfEvidence(unavailable=ENCRYPTED)
            pages = tuple(extract.pdf_page_text(doc[number]) for number in range(min(PAGES_READ, doc.page_count)))
            if not any(extract.has_text_layer(text) for text in pages):
                return PdfEvidence(unavailable=NO_TEXT_LAYER)
            information = {name: value for name, value in (doc.metadata or {}).items() if isinstance(value, str)}
            packet = doc.get_xml_metadata() or ""
    except Exception:  # noqa: BLE001 - PyMuPDF raises many types for damaged input; each means unreadable
        return PdfEvidence(unavailable=UNREADABLE)
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
