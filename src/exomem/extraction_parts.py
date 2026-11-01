"""Hold an extraction that is longer than one page as hidden part pages next to its Source page.

A Source page keeps the first part of its extracted text. Each further part is a page in
a dot folder beside the stored original: for the page `X.md`, part 2 is
`.X.parts/part-02.md`, part 3 is `part-03.md`, and so on. A part names no binary, so no
processing pass ever picks it up. The whole text is the text of part 1, then part 2, and so
on, character for character. Every part is at most `preserve._MAX_EXTRACT_BYTES` of text, so
no page is ever too big to index or to read.

A part is not a memory of its own. It carries `part_of`, the link of its Source page, and
every reader that counts or shows memories folds it into that page. The Source page lists
`extraction_parts` (the number of parts, itself included) and `next_part`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import memory_refs, preserve
from .governance import raw_protection
from .vault import (
    MISSING_CONTENT_HASH,
    PlannedWrite,
    content_hash,
    parse_frontmatter,
    read_guarded_text,
    with_newline,
    yaml_scalar,
)

FOLDER_SUFFIX = ".parts"
_PART_PREFIX = "part-"
#: Front matter of the Source page that describes its parts, and that of a part page.
#: Both are the page schema's own field names, a closed set.
SOURCE_FIELDS = ("extraction_parts", "next_part")
_HEADER_BYTES = 4096


def link_to(vault_root: Path, page: Path) -> str:
    """The wikilink target of a page: its vault-relative path without the extension."""
    return page.relative_to(vault_root).as_posix().removesuffix(".md")


def folder_for(page: Path) -> Path:
    """The folder that holds the further parts of the extraction on `page`.

    It is a dot folder, except for a page that the raw-protection floor marks by the start of
    its name: the floor reads that mark from the start of a path component, and a dot in front
    would hide the folder from it.
    """
    stem = page.name.removesuffix(".md")
    dot = "" if raw_protection.marked(stem) else "."
    return page.with_name(f"{dot}{stem}{FOLDER_SUFFIX}")


def part_path(page: Path, number: int) -> Path:
    return folder_for(page) / f"{_PART_PREFIX}{number:02d}.md"


def source_page_for(part: Path) -> Path | None:
    """The Source page that owns `part`, or None when `part` is not at a part page's path."""
    folder = part.parent
    name = folder.name
    if not (
        name.endswith(FOLDER_SUFFIX)
        and part.name.startswith(_PART_PREFIX)
        and part.name.endswith(".md")
    ):
        return None
    stem = name[: -len(FOLDER_SUFFIX)]
    if raw_protection.marked(stem):
        return folder.with_name(f"{stem}.md")
    return folder.with_name(f"{stem[1:]}.md") if stem.startswith(".") else None


def part_number(part: Path) -> int | None:
    digits = part.name.removeprefix(_PART_PREFIX).removesuffix(".md")
    return int(digits) if digits.isdecimal() else None


def split(text: str) -> list[str]:
    """Cut `text` into pieces of at most one page's bytes, which joined are `text` again.

    A piece ends before a paragraph break when one fits, else before a line break, else at
    a character boundary. The whitespace of the break starts the next piece, so no piece
    ends in whitespace and the first piece of stripped text reads back unchanged.
    """
    limit = preserve._MAX_EXTRACT_BYTES
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return [text]
    text = text.strip()
    pieces: list[str] = []
    start = 0
    start_byte = 0
    raw = text.encode("utf-8")
    while len(raw) - start_byte > limit:
        window = raw[start_byte : start_byte + limit].decode("utf-8", errors="ignore")
        for mark in ("\n\n", "\n"):
            cut = window.rfind(mark)
            if cut > 0:
                break
        else:
            cut = len(window)
        while cut > 1 and window[cut - 1].isspace():
            cut -= 1
        cut = max(cut, 1)
        pieces.append(text[start : start + cut])
        start_byte += len(text[start : start + cut].encode("utf-8"))
        start += cut
    pieces.append(text[start:])
    return pieces


def render_part(
    *,
    artifact_name: str,
    source_link: str,
    inherited: Mapping[str, object],
    number: int,
    total: int,
    text: str,
    next_link: str | None,
    exomem_id: str,
    date_iso: str,
) -> str:
    """The page for part `number` of `total`. `inherited` is the Source page's front matter."""
    title = f"Part {number} of {total}: {artifact_name}"
    lines = ["---"]
    lines += [
        f"{key}: {yaml_scalar(inherited[key])}"
        for key in ("type", "source_type")
        if isinstance(inherited.get(key), str) and inherited[key]
    ]
    listed_tags = inherited.get("tags")
    tags = [tag for tag in listed_tags if isinstance(tag, str)] if isinstance(listed_tags, list) else []
    lines += [
        f"exomem_id: {exomem_id}",
        f"title: {yaml_scalar(title)}",
        f"captured: {date_iso}",
        f"updated: {date_iso}",
        f"part_of: {yaml_scalar(f'[[{source_link}]]')}",
        f"part: {number}",
        f"parts: {total}",
        f"chars: {len(text)}",
    ]
    if next_link is not None:
        lines.append(f"next_part: {yaml_scalar(f'[[{next_link}]]')}")
    lines += [
        "tags: [" + ", ".join(yaml_scalar(tag) for tag in tags) + "]",
        "ingested_into: []",
        "---",
        "",
        f"# {title}",
        "",
        f"Part {number} of {total} of the extracted text of `{source_link}`.",
        "",
        f"{preserve._EXTRACTED_HEADING}\n\n{text}\n",
    ]
    return "\n".join(lines)


def text_of_part(content: str) -> str:
    """The exact text a part page holds, as `render_part` wrote it."""
    content = with_newline(content, "\n")
    marker = f"\n{preserve._EXTRACTED_HEADING}\n\n"
    start = content.find(marker)
    if start == -1:
        return ""
    return content[start + len(marker) :].removesuffix("\n")


def link_target(value: object) -> str | None:
    """The vault-relative page path that a `[[...]]` front matter value names."""
    if not (isinstance(value, str) and value.startswith("[[") and value.endswith("]]")):
        return None
    return value[2:-2] + ".md"


def count_of(frontmatter: Mapping[str, object]) -> int:
    """The number of parts a Source page lists, itself included; 1 when it has no further part."""
    value = frontmatter.get("extraction_parts")
    return value if isinstance(value, int) and value > 1 else 1


def links(vault_root: Path, page: Path, count: int) -> tuple[str, ...]:
    """The links of parts 2 to `count` of `page`, in order."""
    return tuple(link_to(vault_root, part_path(page, number)) for number in range(2, count + 1))


@dataclass(frozen=True)
class Plan:
    """The writes that keep the further parts of one extraction."""

    writes: tuple[PlannedWrite, ...]
    #: Part pages left over from a longer extraction, to remove after the batch commits.
    surplus: tuple[Path, ...]


def plan(
    vault_root: Path,
    page: Path,
    pieces: list[str],
    *,
    inherited: Mapping[str, object],
    date_iso: str,
) -> Plan:
    """Plan the part pages for `pieces[1:]` of the extraction on `page`, replacing any that are there.

    `pieces` come from `split`; `inherited` is the front matter of `page`. Parts already on
    disk are overwritten against their current content; any beyond the new count is `surplus`.
    """
    total = len(pieces)
    artifact_name = page.name.removesuffix(".md")
    source_link = link_to(vault_root, page)
    folder = folder_for(page)
    existing = {
        number: entry
        for entry in (folder.iterdir() if folder.is_dir() else ())
        if (number := part_number(entry)) is not None
    }
    writes: list[PlannedWrite] = []
    for number in range(2, total + 1):
        before = None
        exomem_id = memory_refs.new_id()
        if number in existing:
            before, _guard = read_guarded_text(vault_root, existing[number])
            exomem_id = parse_frontmatter(before)[0].get("exomem_id") or exomem_id
        writes.append(
            PlannedWrite(
                part_path(page, number),
                render_part(
                    artifact_name=artifact_name,
                    source_link=source_link,
                    inherited=inherited,
                    number=number,
                    total=total,
                    text=pieces[number - 1],
                    next_link=link_to(vault_root, part_path(page, number + 1)) if number < total else None,
                    exomem_id=exomem_id,
                    date_iso=date_iso,
                ),
                expected_hash=MISSING_CONTENT_HASH if before is None else content_hash(before),
            )
        )
    surplus = tuple(entry for number, entry in sorted(existing.items()) if number > total)
    return Plan(tuple(writes), surplus)


def retire(vault_root: Path, pages: tuple[Path, ...]) -> None:
    """Remove part pages that no extraction owns any more, and the folder once it is empty."""
    if not pages:
        return
    from . import index_sync

    removed: list[str] = []
    for page in pages:
        try:
            page.unlink()
        except OSError:
            continue
        removed.append(page.relative_to(vault_root).as_posix())
    index_sync.delete_after_remove(vault_root, removed)
    try:
        pages[0].parent.rmdir()
    except OSError:
        pass


def iter_texts(vault_root: Path, page: Path, content: str) -> Iterator[str]:
    """The text of each part of the extraction on `page`, in order, read exactly.

    `content` is the Source page. A part that is missing or unreadable raises `OSError`: a
    document that is short of text must never read as a whole one.
    """
    yield preserve.extracted_text(content)
    for number in range(2, count_of(parse_frontmatter(content)[0]) + 1):
        text, _guard = read_guarded_text(vault_root, part_path(page, number))
        yield text_of_part(text)


def whole_text(vault_root: Path, page: Path, content: str) -> str:
    return "".join(iter_texts(vault_root, page, content))
