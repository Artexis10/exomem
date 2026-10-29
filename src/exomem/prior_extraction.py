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
from pathlib import Path

from . import lifecycle_statuses, memory_refs, preserve
from .vault import parse_frontmatter, with_newline, yaml_scalar

#: Frontmatter that describes an extraction, copied verbatim so every value keeps its YAML type.
#: These are the sidecar schema's own field names, a closed set rather than vocabulary.
_EXTRACTION_FIELDS = frozenset({
    "extracted_by", "processing_state", "processing_attempts", "processing_error",
    "processing_retryable", "processing_next_action", "speakers", "speaker_verification",
})


def extracted_text_of(content: str) -> str:
    """The text of a sidecar's `## Extracted text` section, up to its machine-owned boundary."""
    content = with_newline(content, "\n")
    heading = content.find(preserve._EXTRACTED_HEADING)
    if heading == -1:
        return ""
    start = heading + len(preserve._EXTRACTED_HEADING)
    after = max(
        content.rfind(f"\n{preserve.SIDECAR_PRESERVED_NOTES_SENTINEL}\n", start),
        content.rfind(f"\n{preserve.SIDECAR_ARTIFACT_SENTINEL}\n", start),
    )
    if after == -1:
        legacy = preserve._LEGACY_ARTIFACT_BLOCK_RE.search(content, start)
        notes = content.find("\n## Preserved notes\n", start)
        after = legacy.start() - 1 if legacy else notes if notes != -1 else len(content)
    return content[start:after].strip()


def extraction_digest(content: str) -> str:
    """Identify a sidecar's extraction, so a result can name the one it replaces."""
    engine = parse_frontmatter(content)[0].get("extracted_by")
    return hashlib.sha256(f"{engine}\n{extracted_text_of(content)}".encode()).hexdigest()


def link_to(vault_root: Path, page: Path) -> str:
    """The wikilink target of a page: its vault-relative path without the extension."""
    return page.relative_to(vault_root).as_posix().removesuffix(".md")


def pages_of(vault_root: Path, sidecar_content: str) -> list[Path]:
    """The prior-extraction pages a sidecar lists, in the order they were made."""
    return [vault_root / f"{link}.md" for link in links_of(sidecar_content)]


def links_of(sidecar_content: str) -> tuple[str, ...]:
    listed = parse_frontmatter(sidecar_content)[0].get("prior_extractions")
    if not isinstance(listed, list):
        return ()
    return tuple(
        item.removeprefix("[[").removesuffix("]]")
        for item in listed
        if isinstance(item, str) and item.startswith("[[") and item.endswith("]]")
    )


def next_page(sidecar: Path) -> tuple[Path, int]:
    """The first unused `<sidecar name>.prior-<n>.md` beside the sidecar, with its number."""
    base = sidecar.name.removesuffix(".md")
    ordinal = 1
    while sidecar.with_name(f"{base}.prior-{ordinal}.md").exists():
        ordinal += 1
    return sidecar.with_name(f"{base}.prior-{ordinal}.md"), ordinal


def render(before: str, *, artifact_name: str, sidecar_link: str, ordinal: int, date_iso: str) -> str:
    """The page that keeps the extraction `before` holds, marked as superseded by the sidecar."""
    before = with_newline(before, "\n")
    end = before.find("\n---", 3) if before.startswith("---") else -1
    extraction = [
        line for line in (before[:end].split("\n") if end != -1 else [])
        if line[:1] not in (" ", "-", "\t")
        and (line.partition(":")[0] in _EXTRACTION_FIELDS or line.startswith("asr_"))
    ]
    title = f"Prior extraction {ordinal}: {artifact_name}"
    superseded = lifecycle_statuses.pack_status_for_class("superseded")
    lines = [
        "---",
        "type: source",
        f"exomem_id: {memory_refs.new_id()}",
        f"title: {yaml_scalar(title)}",
        "source_type: other",
        f"captured: {date_iso}",
        f"status: {superseded}",
        "superseded_by:",
        f'  - "[[{sidecar_link}]]"',
        f"updated: {date_iso}",
        *extraction,
        "tags: [prior-extraction]",
        "ingested_into: []",
        "---",
        "",
        f"# {title}",
        "",
        f"This is an earlier extraction of [[{sidecar_link}]]. The recording was processed again, "
        "and the current extraction is on that page.",
        "",
        preserve._EXTRACTED_HEADING,
        "",
        preserve._cap_extracted_text(extracted_text_of(before)),
        "",
    ]
    return "\n".join(lines)
