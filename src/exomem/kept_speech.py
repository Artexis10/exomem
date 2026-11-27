"""Keep a recording's finished transcript when the folder that holds it is adopted.

OpenSpec change `keep-speech-results-in-cloud`, design D1 to D4. A sidecar-named page `X.md` beside the audio or
video original `X` pairs with that recording when it holds a finished extraction of the same bytes. Pairing is by
the page's own `binary_sha256` and `binary_size`, never by its other fields or its path, so a page that carries
provenance fields of its own still pairs.

A page settles on the Source of its recording (`settle`) after that Source is stored: a recording that this
adoption stores is stored first, and one that an earlier window or run stored is found through the duplicate
index. A page whose recording is not stored yet but waits in another window of the same import series is held until
that recording is stored, and files as a text Source only when none is coming (`adoption_run._await_recording`).
`load` reads the page and its part pages and hashes what the plan hashed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `extraction_parts`, `preserve`, `prior_extraction` and `speech_attach` load the media stack, and `adoption_run`
# imports this module, so each function that needs one imports it when called (`tests/test_lazy_imports.py`).
from . import held_fs, lexstore, media_engines, media_processing, media_types, reserved_paths
from .governance import capture_redaction
from .vault import PlannedWrite, content_hash, parse_frontmatter, read_guarded_text

log = logging.getLogger(__name__)

#: The plan action of a page whose result settles on the Source of its recording.
KEEPS = "keeps-extraction"
#: The plan form of that page: it is neither a text Source nor an artifact.
FORM = "kept"

#: Why a page was not paired (the plan's counts), why it waits, and why one failed to settle (an apply outcome's
#: code). These are this change's own codes.
MISMATCH = "KEPT_EXTRACTION_MISMATCH"
INCOMPLETE = "KEPT_EXTRACTION_INCOMPLETE"
PARTS_MISSING = "KEPT_EXTRACTION_PARTS_MISSING"
NO_RECORDING = "KEPT_EXTRACTION_NO_RECORDING"
LOOKUP_UNAVAILABLE = "KEPT_EXTRACTION_LOOKUP_UNAVAILABLE"
RECORDING_PENDING = "KEPT_EXTRACTION_RECORDING_PENDING"
#: The codes of an `apply_stop` that holds pages for their recordings: a later call pairs them, and no person acts.
HOLD_CODES = (RECORDING_PENDING, LOOKUP_UNAVAILABLE)  # nosemgrep: ep-word-set -- two of this module's own protocol codes, not vocabulary.

#: A page and each of its parts are read whole, up to the bound that adoption reads any text to.
_READ_CAP = 4 * 1024 * 1024


class Changed(Exception):
    """The page or a part of it differs from what the plan read."""


class Refused(Exception):
    """The page cannot settle on a stored Source; `code` and `reason` are an outcome's failure."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def candidate(page_rel: str) -> tuple[str, str] | None:
    """The recording a page is named for and its media type, or None when the page's name is no recording's sidecar."""
    if not page_rel.lower().endswith(".md"):
        return None
    original = page_rel[: -len(".md")]
    media_type = media_types.media_type_for(original)
    if media_engines.engine_for(media_type) != media_engines.SPEECH:
        return None
    return original, media_type


def part_folder(page_rel: str) -> str:
    """The vault-relative folder that holds the further parts of the page's extraction."""
    from . import extraction_parts

    return extraction_parts.folder_for(Path(page_rel)).as_posix()


def _read(root: Path, rel: str) -> bytes | None:
    from .media_store import open_original

    try:
        with open_original(root, rel, physical=True) as source:
            if source.version.size > _READ_CAP:
                return None
            return source.stream.read(_READ_CAP + 1)
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return None


@dataclass(frozen=True)
class _Reading:
    """The bytes of one page and its parts, as one read found them."""

    text: str
    fields: Mapping[str, Any]
    #: The text that each part page holds, in order, part 2 first.
    parts: tuple[str, ...]
    #: A digest over the page's bytes and each part page's bytes, so any change to any of them shows.
    digest: str
    size: int


@dataclass(frozen=True)
class Unpaired:
    """A sidecar-named page that did not pair: the reason, and how many part pages it names (they are not kept either)."""

    reason: str
    part_pages: int


@dataclass(frozen=True)
class Examined:
    """A page that holds a kept extraction for the bytes `sha256`/`size`, with the facts the plan records."""

    page: str
    media_type: str
    sha256: str
    size: int
    #: The pages of the extraction, the page itself included (`extraction_parts.count_of`).
    pages: int
    content_sha256: str
    page_sha256: str
    page_bytes: int

    @property
    def part_pages(self) -> int:
        """The further pages that hold the rest of the extraction."""
        return self.pages - 1

    def record(self, *, recording: str | None = None, awaiting: bool = False) -> dict[str, Any]:
        """What the plan keeps about the pairing; `load` finds the page again from it.

        `recording` is the plan's own item for the recording, when this plan stores it. `awaiting` marks a page whose
        recording is stored nowhere yet: its plan item is a text Source that apply files only when no recording comes.
        """
        from . import extraction_parts

        folder = Path(self.page)
        return {
            "page": self.page, "pages": self.pages,
            # The page and each further part page, as the stage holds them: the consume report reads this list.
            "members": [self.page, *(extraction_parts.part_path(folder, n).as_posix() for n in range(2, self.pages + 1))],
            "content_sha256": self.content_sha256, "recording_sha256": self.sha256,
            "recording_bytes": self.size, "media_type": self.media_type, "recording": recording, "awaiting": awaiting,
        }


def _read_parts(root: Path, page_rel: str, count: int) -> tuple[list[bytes], str] | None:
    """The raw bytes of parts 2 to `count`, or None when one is missing, unreadable or no part page."""
    from . import extraction_parts

    raw: list[bytes] = []
    for number in range(2, count + 1):
        data = _read(root, extraction_parts.part_path(Path(page_rel), number).as_posix())
        if data is None:
            return None
        try:
            if extraction_parts.part_document(data.decode("utf-8")) is None:
                return None
        except UnicodeDecodeError:
            return None
        raw.append(data)
    return raw, hashlib.sha256(b"\0".join(raw)).hexdigest()


def _reading(root: Path, page_rel: str, page: bytes, count: int) -> _Reading | None:
    from . import extraction_parts

    parts = _read_parts(root, page_rel, count)
    if parts is None:
        return None
    raw, digest = parts
    text = page.decode("utf-8")
    return _Reading(
        text, parse_frontmatter(text)[0],
        tuple(extraction_parts.text_of_part(data.decode("utf-8")) for data in raw),
        hashlib.sha256(page + b"\0" + digest.encode()).hexdigest(), len(page),
    )


def examine(
    root: Path, page_rel: str, *, media_type: str, expect: tuple[str, int] | None
) -> Examined | Unpaired:
    """Check one sidecar-named page against design D1: the same recording, a finished extraction and all its parts.

    `expect` is the staged original's SHA-256 and size. With none (the recording is not staged) the page's own record
    is what a stored Source must match, and the caller looks that Source up.
    """
    from . import extraction_parts

    data = _read(root, page_rel)
    try:
        text = data.decode("utf-8") if data is not None else None
    except UnicodeDecodeError:
        text = None
    if data is None or text is None:
        return Unpaired(MISMATCH, 0)
    fields = parse_frontmatter(text)[0]
    count = extraction_parts.count_of(fields)
    sha, size = str(fields.get("binary_sha256", "")).lower(), fields.get("binary_size")
    recorded = len(sha) == 64 and isinstance(size, int) and not isinstance(size, bool)
    if (
        fields.get("type") != "source" or fields.get("media_type") != media_type or not recorded
        or expect is not None and (sha, size) != expect
    ):
        return Unpaired(MISMATCH, count - 1)
    if not media_processing.has_completed_transcript(text, media_type=media_type):
        return Unpaired(INCOMPLETE, count - 1)
    reading = _reading(root, page_rel, data, count)
    if reading is None:
        return Unpaired(PARTS_MISSING, count - 1)
    return Examined(
        page_rel, media_type, sha, size, count, reading.digest,
        hashlib.sha256(data).hexdigest(), len(data),
    )


class LookupUnavailable(Exception):
    """The duplicate index cannot say which stored Source holds the recording."""


def find_recording(root: Path, sha256: str, size: int, *, prefer: str | None = None) -> str | None:
    """The vault path of a stored recording whose Source records exactly these bytes, or None when none does.

    `prefer` is a recording that the plan named; it decides alone while its Source still records these bytes, so a
    plan that named its recording applies when the index cannot answer. Otherwise the duplicate index proposes
    candidates and raises `LookupUnavailable` when it cannot answer. A Source's own record of its original's SHA-256 and
    size is its identity, as it is for an attach (`speech_attach.bind`), so no original is read again: a hash of
    every large recording would cost more than the transcript it saves. While the configuration keeps no duplicate
    index the answer is None as well: waiting never changes that, so it is no case for `LookupUnavailable`.
    """
    candidates = [prefer] if prefer else []
    for recording in candidates:
        if _binds(root, recording, sha256, size):
            return recording
    if not lexstore.duplicate_index_configured():
        return None  # the configuration retired the index: no stored recording can be found, now or on a retry
    found = lexstore.payload_candidates(root, sha256)
    if found is None:
        raise LookupUnavailable
    for page in found:
        recording = page[: -len(".md")] if page.lower().endswith(".md") else None
        if recording is not None and recording not in candidates and _binds(root, recording, sha256, size):
            return recording
    return None


def _binds(root: Path, recording: str, sha256: str, size: int) -> bool:
    from . import speech_attach

    try:
        speech_attach.bind(root, speech_attach.resolve(root, recording), sha256, size)
    except media_processing.MediaProcessingError:
        return False
    return True


@dataclass(frozen=True)
class Kept:
    """A page's extraction as apply holds it: the fields, the text with card numbers and credentials removed, and its parts."""

    page: str
    media_type: str
    sha256: str
    size: int
    #: The page's extraction fields, as the page wrote them (`preserve.extraction_field_lines`).
    field_lines: tuple[str, ...]
    engine: str
    #: The first part's text, then each further part's. The text is whole once joined.
    pieces: tuple[str, ...]
    warnings: tuple[str, ...]
    page_text: str

    @property
    def text(self) -> str:
        return "".join(self.pieces)

    @property
    def digest(self) -> str:
        from . import prior_extraction

        return prior_extraction.digest_of(self.engine, self.text)


def load(root: Path, record: Mapping[str, Any]) -> Kept:
    """Read the page that the plan paired, and refuse it when it, or a part, is not what the plan read.

    The kept text passes the card-number and credential removal that every kept text passes
    (`client-artifact-preservation`). A removal changes where the text splits; with none, the page's own parts stay.
    """
    from . import extraction_parts, preserve

    page_rel = record["page"]
    data = _read(root, page_rel)
    reading = None if data is None else _reading(root, page_rel, data, int(record["pages"]))
    if reading is None or reading.digest != record["content_sha256"]:
        raise Changed("the page or one of its parts changed after the plan")
    first = preserve.extracted_text(reading.text)
    pieces = (first, *reading.parts)
    redaction = capture_redaction.redact("".join(pieces))
    warnings: tuple[str, ...] = ()
    if redaction.removed:
        pieces = tuple(extraction_parts.split(redaction.text))
        warnings = (capture_redaction.warning(f"transcript of {page_rel}", redaction.removed),)
    return Kept(
        page_rel, record["media_type"], record["recording_sha256"], record["recording_bytes"],
        tuple(preserve.extraction_field_lines(reading.text)),
        str(reading.fields.get("extracted_by")), pieces, warnings, reading.text,
    )


#: What a transcript page did, as the apply result counts it: filled a Source whose extraction was pending, replaced an
#: unlabelled one, was kept off a Source that holds labels or names, left a Source that already held it as it was,
#: or did not settle.
FILLED, REPLACED, KEPT_OUT, UNCHANGED, FAILED = "filled", "replaced", "kept-out", "unchanged", "failed"  # nosemgrep: ep-word-set -- the closed outcomes of this module's settle step.
OUTCOMES = (FILLED, REPLACED, KEPT_OUT, UNCHANGED, FAILED)  # nosemgrep: ep-word-set -- the closed outcomes of this module's settle step.


@dataclass(frozen=True)
class Settled:
    """What a page's result did on a stored Source."""

    outcome: str
    #: The vault path of the Source page, and of the prior extraction page that the result or the old one went to.
    source_page: str
    prior_page: str | None
    #: The state of the recording's queued speech job after the write (`speech_attach.retire_speech_stage`).
    speech_job: str


def _holds(vault: Path, kept: Kept, sidecar: Path, before: str) -> bool:
    """Whether the Source, or a prior extraction page it lists, already holds the kept result (design D1, step 1)."""
    from . import prior_extraction

    digest = kept.digest
    pages = [(sidecar, before)] + [
        (vault / f"{link}.md", None) for link in prior_extraction.links_of(before)
    ]
    for page, content in pages:
        try:
            if content is None:
                content, _guard = read_guarded_text(vault, page)
            if prior_extraction.whole_extraction_digest(vault, page, content) == digest:
                return True
        except (OSError, ValueError):  # a page or part that does not read is a result that differs
            continue
    return False


def settle(
    vault: Path, kept: Kept, *, recording: str, date_iso: str,
    commit_guard: Callable[[], AbstractContextManager[object]],
) -> Settled:
    """Settle a page's result on the Source of the stored recording, by the one replacement rule (design D1 and D6).

    Writes nothing when the Source or a prior page it lists holds the result. Otherwise the rule decides: a pending or
    failed extraction takes the result, an unlabelled one is replaced and kept on a prior page, and a labelled or named
    one keeps its result while the page's goes to a prior page. Every write is one batch guarded by the Source page's
    content hash, and only the speech stage of a waiting job ends.
    """
    from . import extraction_parts, preserve, prior_extraction, speech_attach

    def unit(commit: Callable[[Any], None]) -> Settled:
        current = speech_attach.resolve(vault, recording)
        speech_attach.bind(vault, current, kept.sha256, kept.size)
        before, guard = read_guarded_text(vault, current.sidecar)
        if _holds(vault, kept, current.sidecar, before):
            return Settled(UNCHANGED, current.sidecar_relative, None, "none")
        links = prior_extraction.links_of(before)
        verdict = prior_extraction.replacement(before, media_type=current.media_type)
        if verdict == prior_extraction.REFUSE:
            prior = prior_extraction.prior_page_writes(
                vault, current.sidecar, before, kept=kept.page_text, text=kept.text, date_iso=date_iso,
                origin=prior_extraction.IMPORTED_AND_DECLINED,
            )
            source = PlannedWrite(
                current.sidecar, preserve.set_prior_extractions(before, (*links, prior.link)),
                expected_hash=content_hash(before), guard=guard,
            )
            commit((source, *prior.writes))
            return Settled(KEPT_OUT, current.sidecar_relative, prior.link + ".md", "none")
        writes: list[PlannedWrite] = []
        prior_link = None
        if verdict == prior_extraction.REPLACE:
            prior = prior_extraction.prior_page_writes(
                vault, current.sidecar, before, kept=before,
                text=extraction_parts.text_with_gaps(vault, current.sidecar, before),
                date_iso=date_iso,
            )
            writes.extend(prior.writes)
            links, prior_link = (*links, prior.link), prior.link + ".md"
        pieces = list(kept.pieces)
        parts = extraction_parts.plan(
            vault, current.sidecar, pieces, inherited=parse_frontmatter(before)[0], date_iso=date_iso
        )
        content = preserve.render_kept_extraction(
            before, field_lines=kept.field_lines, text=pieces[0],
            parts=extraction_parts.layout(current.sidecar, pieces), prior_extractions=links,
        )
        source = PlannedWrite(current.sidecar, content, expected_hash=content_hash(before), guard=guard)
        commit((source, *writes, *parts.writes))
        if parts.surplus:
            speech_attach.retire_parts(vault, parts.surplus)
        job = speech_attach.retire_speech_stage(vault, current)
        return Settled(
            REPLACED if verdict == prior_extraction.REPLACE else FILLED, current.sidecar_relative, prior_link, job
        )

    try:
        return speech_attach.commit_under_guard(vault, commit_guard, "adoption_kept_speech_commit", unit)
    except media_processing.MediaProcessingError as error:
        raise Refused(error.code, str(error)) from error
