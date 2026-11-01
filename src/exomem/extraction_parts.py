"""Hold an extraction that is longer than one page as hidden part pages next to its Source page.

A Source page keeps the first part of its extracted text. Each further part is a page in
a dot folder beside the stored original: for the page `X.md`, part 2 is
`.X.parts/part-02.md`, part 3 is `part-03.md`, and so on. A part names no binary, so no
processing pass ever picks it up. The whole text is the text of part 1, then part 2, and so
on, character for character. Every part is at most `preserve._MAX_EXTRACT_BYTES` of text, so
no page is ever too big to index or to read.

A part is not a memory of its own. It carries `part_of`, and every reader that counts or shows
memories folds it into its Source page. The Source page lists `extraction_parts` (the number of
parts, itself included) and `next_part`.

The links in these fields are paths relative to the page that holds them, not wikilinks: a
wikilink from a part would count as a reference to the Source and block moving it, and a
relative path stays true when the page and its folder move together. A reader finds the Source
of a part from the part's own path (`source_page_for`), never from the field, so a rename that
leaves the field behind breaks nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import media_types, memory_refs, preserve, temporal
from .governance import raw_protection
from .kbdir import kb_dirname
from .vault import (
    MISSING_CONTENT_HASH,
    VAULT_SCAN_SKIP_DIRS,
    PlannedWrite,
    content_hash,
    parse_frontmatter,
    read_guarded_text,
    with_newline,
    yaml_scalar,
)

FOLDER_SUFFIX = ".parts"
_PART_PREFIX = "part-"
#: Front matter of the Source page that describes its parts: how many there are (itself included),
#: where each starts in the whole text, how long the whole text is, and the first further part.
#: A closed set of the page schema's own field names. A reader seeks to any position from
#: these alone, without opening a part.
SOURCE_FIELDS = ("extraction_parts", "extraction_chars", "part_offsets", "next_part")
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
    text = with_newline(text, "\n").strip()
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
    source_name: str,
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
        f"part_of: {yaml_scalar(f'../{source_name}')}",
        f"part: {number}",
        f"parts: {total}",
    ]
    if next_link is not None:
        lines.append(f"next_part: {yaml_scalar(next_link)}")
    lines += [
        "tags: [" + ", ".join(yaml_scalar(tag) for tag in tags) + "]",
        "ingested_into: []",
        "---",
        "",
        f"# {title}",
        "",
        f"Part {number} of {total} of the extracted text of `{source_name}`.",
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


@dataclass(frozen=True)
class Layout:
    """Where the pieces of one extraction sit: what the Source page records about its parts."""

    #: The path of part 2, relative to the Source page's folder.
    next_part: str
    #: Where each part starts in the whole text; the first is 0.
    offsets: tuple[int, ...]
    #: The length of the whole text.
    chars: int


def layout(page: Path, pieces: list[str]) -> Layout | None:
    """The layout of `pieces` on `page`, or None when the text fits on the page alone."""
    if len(pieces) < 2:
        return None
    offsets = [0]
    for piece in pieces:
        offsets.append(offsets[-1] + len(piece))
    return Layout(part_path(page, 2).relative_to(page.parent).as_posix(), tuple(offsets[:-1]), offsets[-1])


def offsets_of(frontmatter: Mapping[str, object]) -> tuple[int, ...] | None:
    """The start of each part that a Source page lists, or None when it lists none."""
    listed = frontmatter.get("part_offsets")
    if isinstance(listed, list) and len(listed) == count_of(frontmatter) and all(isinstance(n, int) for n in listed):
        return tuple(listed)
    return None


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
                    source_name=page.name,
                    inherited=inherited,
                    number=number,
                    total=total,
                    text=pieces[number - 1],
                    next_link=part_path(page, number + 1).name if number < total else None,
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


def clear(vault_root: Path, page: Path) -> None:
    """Remove the part pages of a Source page that is gone, so no part outlives its Source."""
    folder = folder_for(page)
    if folder.is_dir():
        retire(vault_root, tuple(sorted(entry for entry in folder.iterdir() if part_number(entry) is not None)))


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


#: Characters of extracted text one `read_memory` window returns. About 8 thousand tokens: inside
#: the 20 to 50 KB an agent can take in at once, and far below one 512 KB part.
WINDOW_CHARS = 32 * 1024


@dataclass(frozen=True)
class Window:
    """One window of the whole extracted text of a Source, and where it sits."""

    text: str
    offset: int
    #: Where the next window starts; None when this one reaches the end of the text.
    next_offset: int | None
    total_chars: int
    part: int
    parts: int


def _window_end(text: str, start: int) -> int:
    """Where a window that starts at `start` in `text` ends: at a paragraph or line break when it must be cut."""
    end = start + WINDOW_CHARS
    if end >= len(text):
        return len(text)
    window = text[start:end]
    for mark in ("\n\n", "\n"):
        cut = window.rfind(mark)
        if cut > 0:
            return start + cut + len(mark)
    return end


def read_window(
    vault_root: Path, page: Path, frontmatter: Mapping[str, object], first: str, offset: int
) -> Window:
    """The window of the extracted text of `page` that starts at character `offset`.

    `frontmatter` and `first` are the Source page's front matter and its own part of the text.
    A window stays inside one part, so a read never opens more
    than one part page. A window ends at a paragraph or line break; walking `next_offset` from
    0 to None returns the whole text, in order, exactly. Raises `ValueError` for an `offset`
    outside the text and `OSError` for a part page that is missing.
    """
    offsets = offsets_of(frontmatter)
    total = frontmatter.get("extraction_chars") if offsets else len(first)
    if not isinstance(total, int):
        raise ValueError("the page records no length for its extracted text")
    if not 0 <= offset < max(total, 1):
        raise ValueError(f"offset {offset} is outside the extracted text of {total} characters")
    starts = offsets or (0,)
    number = max(index for index, start in enumerate(starts, 1) if start <= offset)
    if number == 1:
        text = first
    else:
        raw, _guard = read_guarded_text(vault_root, part_path(page, number))
        text = text_of_part(raw)
    local = offset - starts[number - 1]
    end = _window_end(text, local)
    reached = starts[number - 1] + end
    return Window(text[local:end], offset, reached if reached < total else None, total, number, len(starts))


@dataclass
class Sweep:
    """What `sweep` found, and with `dry_run=False` fixed."""

    #: Part pages that no Source owns: their Source is gone, or lists fewer parts.
    removed: list[str] = field(default_factory=list)
    #: Source pages whose missing parts were written again from the original.
    rebuilt: list[str] = field(default_factory=list)
    #: Source pages with a part that is missing and could not be rebuilt.
    missing: list[str] = field(default_factory=list)


def _part_folders(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = [child for child in directory.iterdir() if child.is_dir()]
        except OSError:
            continue
        for child in children:
            if child.name in VAULT_SCAN_SKIP_DIRS:
                continue
            if source_page_for(child / f"{_PART_PREFIX}00.md") is not None:
                yield child
            else:
                stack.append(child)


def _rebuild(vault_root: Path, page: Path, content: str, missing: list[int]) -> bool:
    """Write the missing parts of `page` again from its original, when that is cheap and agrees.

    The original is read again only when no speech or vision engine is needed, and its text
    must split into the layout the Source records, so a part never mixes with other text.
    """
    from . import extract

    binary = page.with_name(page.name.removesuffix(".md"))
    kind = media_types.media_type_for(binary)
    if kind is None or kind in media_types.ENGINE_KINDS or not binary.is_file():
        return False
    try:
        pieces = split(extract.extract_text(binary, media_type=kind, vault_root=vault_root).text.strip())
    except Exception:  # noqa: BLE001 — a failed re-read is reported as not rebuilt
        return False
    frontmatter = parse_frontmatter(content)[0]
    agreed = layout(page, pieces)
    if (
        agreed is None
        or pieces[0] != preserve.extracted_text(content)
        or agreed.offsets != offsets_of(frontmatter)
        or agreed.chars != frontmatter.get("extraction_chars")
    ):
        return False
    planned = plan(vault_root, page, pieces, inherited=frontmatter, date_iso=temporal.stamp(temporal.now()))
    writes = tuple(write for write in planned.writes if part_number(write.path) in missing)
    preserve.commit_media_sidecar_writes(vault_root, writes)
    return True


def sweep(vault_root: Path, *, dry_run: bool) -> Sweep:
    """Find part pages with no owner and parts that are missing, and fix them unless `dry_run`.

    A part page belongs to a Source page that exists and lists that many parts. A missing part
    is rebuilt from the original where extraction is cheap; a recording or an image is only
    reported, because rebuilding it would run its engine again.
    """
    result = Sweep()
    for folder in _part_folders(Path(vault_root) / kb_dirname()):
        page = source_page_for(folder / f"{_PART_PREFIX}00.md")
        present = {n: entry for entry in folder.iterdir() if (n := part_number(entry)) is not None}
        try:
            content, _guard = read_guarded_text(vault_root, page)
            count = count_of(parse_frontmatter(content)[0])
        except (OSError, UnicodeError):
            content, count = None, 0
        surplus = tuple(entry for number, entry in sorted(present.items()) if number > count)
        result.removed += [entry.relative_to(vault_root).as_posix() for entry in surplus]
        if not dry_run:
            retire(vault_root, surplus)
        missing = [number for number in range(2, count + 1) if number not in present]
        if content is None or not missing:
            continue
        rel = page.relative_to(vault_root).as_posix()
        if not dry_run and _rebuild(vault_root, page, content, missing):
            result.rebuilt.append(rel)
        else:
            result.missing.append(rel)
    return result
