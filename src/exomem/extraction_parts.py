"""Hold an extraction that is longer than one page as hidden part pages next to its Source page.

A Source page keeps the first part of its extracted text. Each further part is a page in
a dot folder beside the stored original: for the page `X.md`, part 2 is
`.X.parts/part-02.md`, part 3 is `part-03.md`, and so on. A part names no binary, so no
processing pass ever picks it up. The whole text is the text of part 1, then part 2, and so
on, character for character. Every part is at most `preserve._MAX_EXTRACT_BYTES` of text, so
no page is ever too big to index or to read.

A part is not a memory of its own. It carries `part_of`, and every reader that counts or shows
memories folds it into its Source page. The Source page lists `extraction_parts` (the number of
parts, itself included) and `next_part`. Where each part starts in the whole text is never
stored: an edit to any page would make a stored position wrong, so a reader measures the parts
as they are on disk (`part_start`, `read_window`).

The links in these fields are paths relative to the page that holds them, not wikilinks: a
wikilink from a part would count as a reference to the Source and block moving it, and a
relative path stays true when the page and its folder move together. A reader finds the Source
of a part from the part's own path (`source_page_for`), never from the field, so a rename that
leaves the field behind breaks nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import media_engines, media_types, memory_refs, preserve, runtime_resources, temporal
from .governance import raw_protection
from .kbdir import kb_dirname
from .vault import (
    MISSING_CONTENT_HASH,
    PlannedWrite,
    content_hash,
    parse_frontmatter,
    read_frontmatter_header,
    read_guarded_text,
    with_newline,
    yaml_scalar,
)

FOLDER_SUFFIX = ".parts"
_PART_PREFIX = "part-"
#: Front matter of the Source page that describes its parts: how many there are (itself included)
#: and the first further part. A closed set of the page schema's own field names.
SOURCE_FIELDS = ("extraction_parts", "next_part")


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

    A piece ends before the last paragraph break in the second half of its page, else before
    the last line break there, else at the last character that fits. So every piece but the
    last fills at least half a page, and the cut takes time linear in the text.

    The Source page strips the text it holds, so the first piece must not end in whitespace.
    It is cut back to its last other character, which shortens it below half a page only when
    the whole second half of the first page is one run of whitespace.
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
        floor = max(1, len(window) // 2)
        for mark in ("\n\n", "\n"):
            cut = window.rfind(mark, floor)
            if cut != -1:
                break
        else:
            cut = len(window)
        if not pieces:
            cut = len(window[:cut].rstrip()) or cut
        pieces.append(text[start : start + cut])
        start_byte += len(pieces[-1].encode("utf-8"))
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


def count_of(frontmatter: Mapping[str, object]) -> int:
    """The number of parts a Source page lists, itself included; 1 when it has no further part."""
    value = frontmatter.get("extraction_parts")
    return value if isinstance(value, int) and value > 1 else 1


@dataclass(frozen=True)
class Layout:
    """What the Source page records about the parts of its extraction."""

    #: The number of parts, the Source page's own included.
    count: int
    #: The path of part 2, relative to the Source page's folder.
    next_part: str


def layout(page: Path, pieces: list[str]) -> Layout | None:
    """The layout of `pieces` on `page`, or None when the text fits on the page alone."""
    if len(pieces) < 2:
        return None
    return Layout(len(pieces), part_path(page, 2).relative_to(page.parent).as_posix())


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
    from . import file_watcher, index_sync

    # The removal is ours, so the watcher need not report it again (as `delete_file` does for frames).
    try:
        file_watcher.register_self_delete(vault_root, [page.relative_to(vault_root).as_posix() for page in pages])
    except Exception:  # noqa: BLE001 — suppression is best-effort
        pass
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


def carry(vault_root: Path, page_rel: str, new_page_rel: str) -> list[tuple[str, str]]:
    """Move the folder of further parts of the page at `page_rel` to sit beside `new_page_rel`.

    A part belongs to its page, so it goes where the page goes: to Trash, back from it, and on a
    move. Returns each moved part as `(old path, new path)`, empty when the page has no folder of
    parts. Raises `OSError` when the folder cannot move; it then stays where it was.
    """
    from . import file_watcher, reserved_paths
    from .cli_ops import OpError

    old, new = folder_for(Path(page_rel)), folder_for(Path(new_page_rel))
    if not (vault_root / old).is_dir():
        return []
    moved = [
        ((old / entry.name).as_posix(), (new / entry.name).as_posix())
        for entry in sorted((vault_root / old).iterdir())
        if part_number(entry) is not None
    ]
    # The removal is ours, so the watcher need not report it again.
    try:
        file_watcher.register_self_delete(vault_root, [before for before, _after in moved])
    except Exception:  # noqa: BLE001 — suppression is best-effort
        pass
    try:
        reserved_paths.move_generic_path(
            vault_root, old.as_posix(), new.as_posix(), source_kind="directory", physical=True
        )
    except (OpError, reserved_paths.ReservedPathLeafError) as error:
        raise OSError(f"the part pages in {old.as_posix()} could not move to {new.as_posix()}") from error
    return moved


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


def text_with_gaps(vault_root: Path, page: Path, content: str) -> str:
    """The whole text as far as it reads, with a visible line in place of each part that does not.

    Only for keeping an extraction that a new one replaces: a missing part must not stop the
    new extraction, which is how the operator recovers the text.
    """
    texts = [preserve.extracted_text(content)]
    for number in range(2, count_of(parse_frontmatter(content)[0]) + 1):
        try:
            raw, _guard = read_guarded_text(vault_root, part_path(page, number))
        except (OSError, UnicodeError):
            texts.append(f"\n\n[part {number} of this extraction was missing when it was replaced]\n\n")
            continue
        texts.append(text_of_part(raw))
    return "".join(texts)


#: The length of each part page's text and the part count of each Source page read so far,
#: keyed by the file's stat, so a reader measures a page again only after it changed.
_LENGTHS: dict[Path, tuple[tuple[int, int, int], int]] = {}
_COUNTS: dict[Path, tuple[tuple[int, int, int], int]] = {}
#: Each memo is emptied once it holds this many pages.
_MEMO_MAX = 4096


def _by_stat(memo: dict[Path, tuple[tuple[int, int, int], int]], path: Path, measure: Callable[[], int]) -> int:
    """`measure()` for the file at `path` as it is now, measured again only when its stat changed."""
    status = path.stat()
    stamp = (status.st_ino, status.st_mtime_ns, status.st_size)
    cached = memo.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    value = measure()
    if len(memo) >= _MEMO_MAX:
        memo.clear()
    memo[path] = (stamp, value)
    return value


def _part_text(vault_root: Path, page: Path, number: int) -> str:
    raw, _guard = read_guarded_text(vault_root, part_path(page, number))
    return text_of_part(raw)


def _part_length(vault_root: Path, page: Path, number: int) -> int:
    """The length of the text of part `number` as it is now. Raises `OSError` when it is missing."""
    return _by_stat(_LENGTHS, part_path(page, number), lambda: len(_part_text(vault_root, page, number)))


def listed(part: Path) -> bool:
    """Whether `part` is one of the parts its Source page lists now.

    A part left over from a longer extraction is not: search ignores it and `sweep` removes it.
    """
    source = source_page_for(part)
    number = part_number(part)
    if source is None or number is None or number < 2:
        return False
    try:
        return number <= _by_stat(_COUNTS, source, lambda: _declared_count(source))
    except OSError:
        return False


def _bounds(vault_root: Path, page: Path, first: str, count: int) -> list[int]:
    """Where each part starts in the whole text as it is now, then where the text ends."""
    bounds = [0, len(first)]
    for number in range(2, count + 1):
        bounds.append(bounds[-1] + _part_length(vault_root, page, number))
    return bounds


def part_start(vault_root: Path, page: Path, first: str, number: int) -> int:
    """Where part `number` of the extraction on `page` starts in the whole text, measured now.

    `first` is the Source page's own part of the text. Raises `OSError` for a missing part.
    """
    return _bounds(vault_root, page, first, number)[number - 1] if number > 1 else 0


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


def without_spans(text: str, start: int, end: int, spans: Iterable[tuple[int, int]]) -> str:
    """`text[start:end]` without the characters of `spans`, which are positions in all of `text`."""
    from . import provenance

    clipped = [(max(s, start) - start, min(e, end) - start) for s, e in spans if s < end and e > start]
    return provenance.remove_carriers(text[start:end], clipped) if clipped else text[start:end]


def read_window(
    vault_root: Path,
    page: Path,
    frontmatter: Mapping[str, object],
    first: str,
    offset: int,
    *,
    withhold: Callable[[str], Iterable[tuple[int, int]]] = lambda _text: (),
) -> Window:
    """The window of the extracted text of `page` that starts at character `offset`.

    `frontmatter` and `first` are the Source page's front matter and its own part of the text.
    Positions are measured on the parts as they are now, so an edit to any page never leaves a
    wrong one. A window stays inside one part and ends at a paragraph or line break;
    `next_offset` is always past `offset`, and walking it from 0 to None returns the text of
    every part, in order, exactly. `withhold` gives the spans of a part's text that the caller
    is not shown; they leave the window's text, never its positions. Raises `ValueError` for
    an `offset` outside the text and `OSError` for a part page that is missing or that changes
    during the read.
    """
    count = count_of(frontmatter)
    for _attempt in range(2):
        bounds = _bounds(vault_root, page, first, count)
        total = bounds[-1]
        if not 0 <= offset < max(total, 1):
            raise ValueError(f"offset {offset} is outside the extracted text of {total} characters")
        # The first part that ends past `offset`; an empty part holds no position.
        number = next((n for n in range(1, count + 1) if offset < bounds[n]), 1)
        text = first if number == 1 else _part_text(vault_root, page, number)
        if len(text) == bounds[number] - bounds[number - 1]:
            break
    else:
        raise OSError(f"part {number} changed while it was read")
    local = offset - bounds[number - 1]
    end = _window_end(text, local)
    reached = bounds[number - 1] + end
    shown = without_spans(text, local, end, withhold(text))
    return Window(shown, offset, reached if reached < total else None, total, number, count)


@dataclass
class Sweep:
    """What `sweep` found, and with `dry_run=False` fixed."""

    #: Part pages left over from a longer extraction of a Source page that now lists fewer parts.
    removed: list[str] = field(default_factory=list)
    #: Source pages whose missing parts were written again from the original.
    rebuilt: list[str] = field(default_factory=list)
    #: Source pages with a part that is missing and could not be rebuilt.
    missing: list[str] = field(default_factory=list)
    #: Pages at a part's path that the sweep leaves alone: their Source page is gone or holds no
    #: extraction, or the page is not a part written for it. A person decides about these.
    unowned: list[str] = field(default_factory=list)


def _declared_count(path: Path) -> int:
    """The number of parts the page at `path` lists, read from its front matter alone."""
    try:
        with path.open("rb") as stream:
            header = read_frontmatter_header(stream, runtime_resources.SEMANTIC_SOURCE_MAX_BYTES)
        return count_of(parse_frontmatter(header.decode("utf-8"))[0])
    except (OSError, ValueError):  # an unreadable page lists nothing; its own readers report it
        return 1


def _written_as_part(vault_root: Path, entry: Path, number: int) -> bool:
    """Whether `entry` is the part page `render_part` wrote as part `number`, not a hand-made file."""
    try:
        content, _guard = read_guarded_text(vault_root, entry)
    except (OSError, UnicodeError):
        return False
    frontmatter = parse_frontmatter(content)[0]
    return frontmatter.get("part") == number and isinstance(frontmatter.get("part_of"), str)


def _rebuild(vault_root: Path, page: Path, content: str, present: Mapping[int, Path], missing: list[int]) -> bool:
    """Write the missing parts of `page` again from its original, when that is cheap and agrees.

    The original is read again only with the engines that run no model, so a recording, an
    image or a scanned PDF page is never transcribed again. Its text must split into the parts
    the Source page holds now, so a rebuilt part never mixes with other text.
    """
    from . import extract

    binary = page.with_name(page.name.removesuffix(".md"))
    kind = media_types.media_type_for(binary)
    if kind is None or media_engines.engine_for(kind) in media_engines.MODEL_ENGINES or not binary.is_file():
        return False
    try:
        with media_engines.only(set(media_engines.ENGINES) - media_engines.MODEL_ENGINES):
            pieces = split(extract.extract_text(binary, media_type=kind, vault_root=vault_root).text.strip())
        agreed = len(pieces) == count_of(parse_frontmatter(content)[0]) and pieces[0] == preserve.extracted_text(content)
        agreed = agreed and all(_part_text(vault_root, page, number) == pieces[number - 1] for number in present)
    except Exception:  # noqa: BLE001 — a failed re-read is reported as not rebuilt
        return False
    if not agreed:
        return False
    planned = plan(
        vault_root, page, pieces, inherited=parse_frontmatter(content)[0], date_iso=temporal.stamp(temporal.now())
    )
    preserve.commit_media_sidecar_writes(
        vault_root, tuple(write for write in planned.writes if part_number(write.path) in missing)
    )
    return True


def sweep(vault_root: Path, *, dry_run: bool) -> Sweep:
    """Check every Source page that lists parts and every page at a part's path; fix unless `dry_run`.

    A missing part is rebuilt from the original where that is cheap (`_rebuild`) and reported
    otherwise, also when the whole folder is gone. A part beyond the number its Source page
    lists is removed, but only when that Source page holds an extraction and the part is one
    `render_part` wrote; every other page at a part's path is reported, never removed.
    """
    from . import find_corpus

    root = Path(vault_root)
    result = Sweep()
    declared: set[Path] = set()
    found: dict[Path, dict[int, Path]] = {}
    for path in find_corpus.walk_md(root / kb_dirname()):
        owner = source_page_for(path)
        if owner is None:
            if _declared_count(path) > 1:
                declared.add(path)
        elif (number := part_number(path)) is not None:
            found.setdefault(owner, {})[number] = path
    for page in sorted(declared | set(found)):
        present = found.get(page, {})
        try:
            content, _guard = read_guarded_text(root, page)
        except (OSError, UnicodeError):
            content = None
        if content is None or preserve._EXTRACTED_HEADING not in content:
            result.unowned += [entry.relative_to(root).as_posix() for _n, entry in sorted(present.items())]
            continue
        count = count_of(parse_frontmatter(content)[0])
        surplus: list[Path] = []
        for number, entry in sorted(present.items()):
            if 2 <= number <= count:
                continue
            if _written_as_part(root, entry, number):
                surplus.append(entry)
            else:
                result.unowned.append(entry.relative_to(root).as_posix())
        result.removed += [entry.relative_to(root).as_posix() for entry in surplus]
        if not dry_run:
            retire(root, tuple(surplus))
        missing = [number for number in range(2, count + 1) if number not in present]
        if not missing:
            continue
        kept = {number: entry for number, entry in present.items() if 2 <= number <= count}
        rel = page.relative_to(root).as_posix()
        if not dry_run and _rebuild(root, page, content, kept, missing):
            result.rebuilt.append(rel)
        else:
            result.missing.append(rel)
    return result
