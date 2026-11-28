"""Keep the extraction that a reprocess replaces, as its own page next to the sidecar.

A sidecar holds one extraction. When a caller asks for a recording to be processed
again, the new text replaces the old in the sidecar and the old text moves, in the
same atomic write, to `<sidecar name>.prior-<n>.md`. That page carries the old
extraction fields, is marked with the lifecycle status of the superseded class so
retrieval demotes it, and is not a media item: it names no binary, so no processing
pass ever picks it up. The sidecar lists these pages in `prior_extractions`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from . import extraction_parts, lifecycle_statuses, memory_refs, preserve, speaker_names, temporal
from .asr_facts import AsrFacts
from .capture_metadata import CaptureFacts
from .extraction_parts import Layout
from .ocr_facts import OcrFacts
from .speech_tracks import Track
from .vault import MISSING_CONTENT_HASH, PathGuard, PlannedWrite, parse_frontmatter, with_newline, yaml_scalar


def extraction_digest(content: str) -> str:
    """Identify a sidecar's extraction, so a result can name the one it replaces."""
    engine = parse_frontmatter(content)[0].get("extracted_by")
    return hashlib.sha256(f"{engine}\n{preserve.extracted_text(content)}".encode()).hexdigest()


def whole_extraction_digest(vault_root: Path, page: Path, content: str) -> str:
    """Identify the whole extraction of a Source page: its engine and the text of every part.

    `extraction_digest` reads the first part alone, which names what a result replaces on one page.
    This one lets a caller ask whether a stored page and an incoming one hold the same result.
    The adoption of a Source (`kept_speech`) is its first caller.
    """
    engine = parse_frontmatter(content)[0].get("extracted_by")
    return digest_of(engine, extraction_parts.whole_text(vault_root, page, content))


def digest_of(engine: object, whole_text: str) -> str:
    """The digest of an extraction that a caller holds as its engine and its whole text (`whole_extraction_digest`)."""
    return hashlib.sha256(f"{engine}\n{whole_text}".encode()).hexdigest()


#: What a speech result does to the extraction that a Source holds (`replacement`).
FILL = "fill"  # the page holds no finished extraction: the result becomes it, and nothing is kept
REPLACE = "replace"  # a finished extraction: the result replaces it, and the old one stays on a prior page
REFUSE = "refuse"  # a labelled extraction and no leave to replace it: the result changes nothing


def replacement(content: str, *, media_type: str, replace_labelled: bool = False) -> str:
    """The one rule for whether a speech result replaces the extraction on this Source page.

    A pending or failed extraction is filled. A finished one with no speaker labels and no
    `speaker_names`, such as the cell's own transcript, is replaced and kept on a prior page. A
    finished one with either is replaced only when the caller says so, since a person's naming
    or a diarisation costs more than the text it labels.
    """
    from . import media_processing

    if not media_processing.has_completed_transcript(content, media_type=media_type):
        return FILL
    frontmatter = parse_frontmatter(content)[0]
    labelled = bool(frontmatter.get("speakers")) or bool(frontmatter.get("speaker_names"))
    return REPLACE if replace_labelled or not labelled else REFUSE


def names_of(content: str) -> list[dict[str, str]]:
    """The `speaker_names` entries of a sidecar, each with its name, label and basis."""
    listed = parse_frontmatter(content)[0].get("speaker_names")
    if not isinstance(listed, list):
        return []
    return [
        {key: str(item[key]) for key in ("name", "label", "basis")}
        for item in listed
        if isinstance(item, dict) and all(isinstance(item.get(key), str) for key in ("name", "label", "basis"))
    ]


def link_to(vault_root: Path, page: Path) -> str:
    """The wikilink target of a page: its vault-relative path without the extension."""
    return page.relative_to(vault_root).as_posix().removesuffix(".md")


def links_of(sidecar_content: str) -> tuple[str, ...]:
    listed = parse_frontmatter(sidecar_content)[0].get("prior_extractions")
    if not isinstance(listed, list):
        return ()
    return tuple(
        item.removeprefix("[[").removesuffix("]]")
        for item in listed
        if isinstance(item, str) and item.startswith("[[") and item.endswith("]]")
    )


def next_page(sidecar: Path, listed: Iterable[str] = ()) -> tuple[Path, int]:
    """The `<sidecar name>.prior-<n>.md` page after every one that exists or that the sidecar lists.

    A page the user deleted keeps its number, so a later page never takes its place.
    """
    base = sidecar.name.removesuffix(".md")
    prefix = f"{base}.prior-"
    names = [Path(link).name for link in listed]
    names += [entry.name.removesuffix(".md") for entry in sidecar.parent.iterdir()]
    ordinals = [
        int(name.removeprefix(prefix))
        for name in names
        if name.startswith(prefix) and name.removeprefix(prefix).isdecimal()
    ]
    ordinal = max(ordinals, default=0) + 1
    return sidecar.with_name(f"{prefix}{ordinal}.md"), ordinal


#: Why a prior page exists: the recording was processed again, or an imported page's result lost to a labelled one.
REPROCESSED = "reprocessed"
IMPORTED_AND_DECLINED = "imported-and-declined"
_ORIGIN_SENTENCES = {
    REPROCESSED: (
        "This is an earlier extraction of [[{link}]]. The recording was processed again, "
        "and the current extraction is on that page."
    ),
    IMPORTED_AND_DECLINED: (
        "This is an extraction of the recording of [[{link}]] that an imported page held. That Source already "
        "holds speaker labels or names, so its own extraction stays there and this one is kept here."
    ),
}


def render(
    before: str,
    *,
    artifact_name: str,
    sidecar_link: str,
    ordinal: int,
    date_iso: str,
    text: str,
    parts: Layout | None = None,
    origin: str = REPROCESSED,
) -> str:
    """The page that keeps the extraction `before` holds, marked as superseded by the sidecar.

    The page is a former extraction of the same source, so it takes the sidecar's own
    `type`, `source_type` and `tags`. `text` is the first part of that extraction and `parts`
    the layout of the part pages that hold the rest, as `extraction_parts.layout` gives it.
    `origin` says why the page exists (`REPROCESSED` or `IMPORTED_AND_DECLINED`).
    """
    before = with_newline(before, "\n")
    frontmatter = parse_frontmatter(before)[0]
    extraction = preserve.extraction_field_lines(before)
    inherited = [
        f"{key}: {yaml_scalar(frontmatter[key])}"
        for key in ("type", "source_type")
        if isinstance(frontmatter.get(key), str) and frontmatter[key]
    ]
    listed_tags = frontmatter.get("tags")
    tags = [tag for tag in listed_tags if isinstance(tag, str)] if isinstance(listed_tags, list) else []
    title = f"Prior extraction {ordinal}: {artifact_name}"
    superseded = lifecycle_statuses.pack_status_for_class("superseded")
    lines = [
        "---",
        *inherited,
        f"exomem_id: {memory_refs.new_id()}",
        f"title: {yaml_scalar(title)}",
        f"captured: {date_iso}",
        f"status: {superseded}",
        "superseded_by:",
        f'  - "[[{sidecar_link}]]"',
        f"updated: {date_iso}",
        *extraction,
        *preserve._part_field_lines(parts),
        "tags: [" + ", ".join(yaml_scalar(tag) for tag in tags) + "]",
        "ingested_into: []",
        "---",
        "",
        f"# {title}",
        "",
        _ORIGIN_SENTENCES[origin].format(link=sidecar_link),
        "",
        preserve._EXTRACTED_HEADING,
        "",
        text,
        "",
    ]
    withheld = preserve.low_confidence_text(before)
    if withheld:
        # The words OCR withheld stay with the extraction they belong to.
        lines += [preserve.low_confidence_block(preserve._cap_low_confidence_text(withheld))]
    return "\n".join(lines)


@dataclass(frozen=True)
class PriorPage:
    """A prior extraction page to create: where it goes, the link that the Source lists, and the writes that make it."""

    path: Path
    link: str
    #: The page, then the part pages that hold the rest of its text.
    writes: tuple[PlannedWrite, ...]


def prior_page_writes(
    vault_root: Path, sidecar_path: Path, before: str, *, kept: str, text: str, date_iso: str,
    origin: str = REPROCESSED,
) -> PriorPage:
    """The next `<sidecar name>.prior-<n>.md` page for the extraction that `kept` carries, with its part pages.

    `before` is the Source page that the prior page belongs to and `sidecar_path` its path. `kept` is the page
    whose extraction fields the prior page copies (the Source itself on a reprocess, an imported page when its
    result lost to a labelled one) and `text` is that extraction in full.
    """
    page, ordinal = next_page(sidecar_path, links_of(before))
    pieces = extraction_parts.split(text)
    parts = extraction_parts.plan(vault_root, page, pieces, inherited=parse_frontmatter(kept)[0], date_iso=date_iso)
    content = render(
        kept,
        artifact_name=sidecar_path.name.removesuffix(".md"),
        sidecar_link=link_to(vault_root, sidecar_path),
        ordinal=ordinal,
        date_iso=date_iso,
        text=pieces[0],
        parts=extraction_parts.layout(page, pieces),
        origin=origin,
    )
    return PriorPage(
        page, link_to(vault_root, page),
        (PlannedWrite(page, content, expected_hash=MISSING_CONTENT_HASH), *parts.writes),
    )


def extraction_writes(
    vault_root: Path,
    sidecar_path: Path,
    before: str,
    guard: PathGuard,
    *,
    binary_path: Path,
    text: str,
    engine: str,
    speakers: list[dict] | None,
    speaker_verification: str | None,
    diarize: bool | None,
    attempts: int | None,
    asr: AsrFacts | None,
    ocr: OcrFacts | None,
    capture: CaptureFacts | None,
    reprocessing: bool,
    attached_digest: str | None = None,
    given_names: list[dict[str, str]] | None = None,
    speech_tracks: tuple[Track, ...] | None = None,
) -> tuple[tuple[PlannedWrite, ...], tuple[Path, ...]]:
    """The writes for a finished extraction, and the part pages to retire once they commit.

    The first write is the sidecar. It holds the first part of the text; the rest is on
    part pages in the same batch. A reprocess adds the pages for the old extraction, with its
    parts, so the sidecar always holds one extraction, and a rejected batch leaves the old
    text where it was. The worker publishes its results here, and an attached result goes the
    same way (`speech_attach`).

    `given_names` are the names the caller already put on the labels of `text`. A reprocess
    carries the replaced extraction's other names to the new labels by overlap, and the page
    lists the names that no label took (`speaker_names.carry`).
    `speech_tracks` is the declaration of tracks that a request gave this extraction; the sidecar records it.
    """
    from . import media_processing

    fields: dict[str, object] = dict(
        engine=engine,
        diarize=diarize,
        attempts=attempts,
        asr=asr,
        ocr=ocr,
        capture=capture,
        attached_digest=attached_digest,
        speech_tracks=speech_tracks,
        # Both callers have just revalidated the original; a page stored with it records these.
        binary_hints=media_processing.read_timestamp_hints(media_processing.held_path(vault_root, binary_path)),
    )
    entries = list(given_names or [])
    if not reprocessing:
        speakers, verification = _after_naming(text, speakers, speaker_verification, entries)
        plan, write = preserve.sidecar_extraction_writes(
            vault_root, sidecar_path, before, guard, text=text, speakers=speakers,
            speaker_verification=verification, speaker_names=entries or None, **fields,
        )
        return (write, *plan.writes), plan.surplus
    old_text = extraction_parts.text_with_gaps(vault_root, sidecar_path, before)
    prior = prior_page_writes(
        vault_root, sidecar_path, before, kept=before, text=old_text, date_iso=temporal.stamp(temporal.now()),
    )
    given = {entry["name"] for entry in entries}  # the call's names already stand on the labels of `text`
    carried = speaker_names.carry(
        old_text, [entry for entry in names_of(before) if entry["name"] not in given], text, keep_labels=given
    )
    if carried.labels:
        text = speaker_names.rename_labels(text, carried.labels)
        entries += carried.entries
    speakers, verification = _after_naming(text, speakers, speaker_verification, entries, carried.labels)
    plan, write = preserve.sidecar_extraction_writes(
        vault_root, sidecar_path, before, guard, text=text, speakers=speakers,
        speaker_verification=verification, speaker_names=entries or None,
        names_not_carried=list(carried.not_carried) or None,
        prior_extractions=(*links_of(before), prior.link),
        reset_speakers=True,
        **fields,
    )
    return (write, *prior.writes, *plan.writes), plan.surplus


def _after_naming(
    text: str,
    speakers: list[dict] | None,
    verification: str | None,
    entries: list[dict[str, str]],
    renamed: dict[str, str] | None = None,
) -> tuple[list[dict] | None, str | None]:
    """The turns and `speaker_verification` after names went onto the labels of `text`.

    `renamed` is the label-to-name map that the caller applied to `text` after the turns were read,
    so the turns say the names too. A person who named every label makes the extraction `human-verified`.
    """
    if renamed and speakers:
        speakers = [{**turn, "speaker": renamed.get(turn.get("speaker"), turn.get("speaker"))} for turn in speakers]
    return speakers, speaker_names.with_names(speaker_names.labels_in(text), entries, verification)
