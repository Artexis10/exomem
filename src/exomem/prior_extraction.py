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
from pathlib import Path

from . import lifecycle_statuses, memory_refs, preserve
from .vault import parse_frontmatter, with_newline, yaml_scalar


def extraction_digest(content: str) -> str:
    """Identify a sidecar's extraction, so a result can name the one it replaces."""
    engine = parse_frontmatter(content)[0].get("extracted_by")
    return hashlib.sha256(f"{engine}\n{preserve.extracted_text(content)}".encode()).hexdigest()


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


def render(before: str, *, artifact_name: str, sidecar_link: str, ordinal: int, date_iso: str) -> str:
    """The page that keeps the extraction `before` holds, marked as superseded by the sidecar.

    The page is a former extraction of the same source, so it takes the sidecar's own
    `type`, `source_type` and `tags`.
    """
    before = with_newline(before, "\n")
    frontmatter = parse_frontmatter(before)[0]
    end = before.find("\n---", 3) if before.startswith("---") else -1
    extraction = [
        line for line in (before[:end].split("\n") if end != -1 else [])
        if line[:1] not in (" ", "-", "\t") and preserve.is_extraction_field(line.partition(":")[0])
    ]
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
        "tags: [" + ", ".join(yaml_scalar(tag) for tag in tags) + "]",
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
        preserve._cap_extracted_text(preserve.extracted_text(before)),
        "",
    ]
    withheld = preserve.low_confidence_text(before)
    if withheld:
        # The words OCR withheld stay with the extraction they belong to.
        lines += [preserve.low_confidence_block(preserve._cap_extracted_text(withheld))]
    return "\n".join(lines)
